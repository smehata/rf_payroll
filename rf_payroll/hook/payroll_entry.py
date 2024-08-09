import frappe
import erpnext
from frappe import _
from frappe.utils import flt
from hrms.payroll.doctype.payroll_entry.payroll_entry import PayrollEntry
from hrms.payroll.doctype.salary_withholding.salary_withholding import link_bank_entry_in_salary_withholdings
from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import (
    get_accounting_dimensions,
)
from frappe.query_builder.functions import Sum


class OvrPayrollEntry(PayrollEntry):

    @frappe.whitelist()
    def make_bank_entry(self, for_withheld_salaries=False):
        self.check_permission("write")
        self.employee_based_payroll_payable_entries = {}
        employee_wise_accounting_enabled = frappe.db.get_single_value(
            "Payroll Settings", "process_payroll_accounting_entry_based_on_employee"
        )

        salary_slip_total = 0
        salary_slips = self.get_salary_slip_details(for_withheld_salaries)

        for salary_detail in salary_slips:
            if salary_detail.parentfield == "earnings":
                (
                    is_flexible_benefit,
                    only_tax_impact,
                    create_separate_je,
                    statistical_component,
                ) = frappe.db.get_value(
                    "Salary Component",
                    salary_detail.salary_component,
                    (
                        "is_flexible_benefit",
                        "only_tax_impact",
                        "create_separate_payment_entry_against_benefit_claim",
                        "statistical_component",
                    ),
                    cache=True,
                )

                if only_tax_impact != 1 and statistical_component != 1:
                    if is_flexible_benefit == 1 and create_separate_je == 1:
                        self.set_accounting_entries_for_bank_entry(
                            salary_detail.amount, salary_detail.salary_component
                        )
                    else:
                        if employee_wise_accounting_enabled:
                            self.set_employee_based_payroll_payable_entries(
                                "earnings",
                                salary_detail.employee,
                                salary_detail.amount,
                                salary_detail.salary_structure,
                            )
                        salary_slip_total += salary_detail.amount

            if salary_detail.parentfield == "deductions":
                statistical_component = frappe.db.get_value(
                    "Salary Component", salary_detail.salary_component, "statistical_component", cache=True
                )

                if not statistical_component:
                    if employee_wise_accounting_enabled:
                        self.set_employee_based_payroll_payable_entries(
                            "deductions",
                            salary_detail.employee,
                            salary_detail.amount,
                            salary_detail.salary_structure,
                        )

                    salary_slip_total -= salary_detail.amount
        ss = self.loan_deduction_amount()
        salary_slip_total -= self.loan_deduction_amount()
        if salary_slip_total > 0:
            remark = "withheld salaries" if for_withheld_salaries else "salaries"
            bank_entry = self.set_accounting_entries_for_bank_entry(salary_slip_total, remark)

        if for_withheld_salaries:
            link_bank_entry_in_salary_withholdings(salary_slips, bank_entry.name)

        return bank_entry

    def set_accounting_entries_for_bank_entry(self, je_payment_amount, user_remark):
        payroll_payable_account = self.payroll_payable_account
        precision = frappe.get_precision("Journal Entry Account", "debit_in_account_currency")

        accounts = []
        currencies = []
        company_currency = erpnext.get_company_currency(self.company)
        accounting_dimensions = get_accounting_dimensions() or []

        exchange_rate, amount = self.get_amount_and_exchange_rate_for_journal_entry(
            self.payment_account, je_payment_amount, company_currency, currencies
        )
        accounts.append(
            self.update_accounting_dimensions(
                {
                    "account": self.payment_account,
                    "bank_account": self.bank_account,
                    "credit_in_account_currency": flt(amount, precision),
                    "exchange_rate": flt(exchange_rate),
                    "cost_center": self.cost_center,
                },
                accounting_dimensions,
            )
        )

        if self.employee_based_payroll_payable_entries:
            for employee, employee_details in self.employee_based_payroll_payable_entries.items():
                je_payment_amount = employee_details.get("earnings", 0) - (
                    employee_details.get("deductions", 0)
                )
                loan_amt = frappe.get_value("Salary Slip", {"employee": employee, "payroll_entry": self.name}, "total_loan_repayment")
                if loan_amt > 0:
                    je_payment_amount -= loan_amt

                exchange_rate, amount = self.get_amount_and_exchange_rate_for_journal_entry(
                    self.payment_account, je_payment_amount, company_currency, currencies
                )

                cost_centers = self.get_payroll_cost_centers_for_employee(
                    employee, employee_details.get("salary_structure")
                )

                for cost_center, percentage in cost_centers.items():
                    amount_against_cost_center = flt(amount) * percentage / 100
                    accounts.append(
                        self.update_accounting_dimensions(
                            {
                                "account": payroll_payable_account,
                                "debit_in_account_currency": flt(amount_against_cost_center, precision),
                                "exchange_rate": flt(exchange_rate),
                                "reference_type": self.doctype,
                                "reference_name": self.name,
                                "party_type": "Employee",
                                "party": employee,
                                "cost_center": cost_center,
                            },
                            accounting_dimensions,
                        )
                    )
        else:
            exchange_rate, amount = self.get_amount_and_exchange_rate_for_journal_entry(
                payroll_payable_account, je_payment_amount, company_currency, currencies
            )
            accounts.append(
                self.update_accounting_dimensions(
                    {
                        "account": payroll_payable_account,
                        "debit_in_account_currency": flt(amount, precision),
                        "exchange_rate": flt(exchange_rate),
                        "reference_type": self.doctype,
                        "reference_name": self.name,
                        "cost_center": self.cost_center,
                    },
                    accounting_dimensions,
                )
            )

        return self.make_journal_entry(
            accounts,
            currencies,
            voucher_type="Bank Entry",
            user_remark=_("Payment of {0} from {1} to {2}").format(
                _(user_remark), self.start_date, self.end_date
            ),
        )

    def loan_deduction_amount(self):
        SalarySlip = frappe.qb.DocType("Salary Slip")
        query = (
            frappe.qb.from_(SalarySlip)
                .select(Sum(SalarySlip.total_loan_repayment))
                .where(
                (SalarySlip.docstatus == 1)
                & (SalarySlip.start_date >= self.start_date)
                & (SalarySlip.end_date <= self.end_date)
                & (SalarySlip.payroll_entry == self.name)
            )
        )
        return flt(query.run()[0][0] or 0)