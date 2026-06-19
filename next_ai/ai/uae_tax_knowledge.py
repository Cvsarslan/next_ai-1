"""Curated UAE tax context for the portal assistant.

This is a concise orientation layer, not a replacement for current legislation or
case-specific professional advice. Official source links are included so the model
can direct users to the controlling guidance.
"""

UAE_TAX_KNOWLEDGE_VERSION = "2026-06-19"

UAE_TAX_KNOWLEDGE = r"""
UAE TAX KNOWLEDGE (reviewed 19 June 2026; use as general guidance only)

VAT
- UAE VAT is a transaction-based consumption tax. The standard rate is 5%; qualifying
  supplies may be zero-rated, while exempt supplies are treated differently. Never
  infer zero-rating or exemption merely from a product label; place of supply, customer,
  evidence, and statutory conditions matter.
- Resident businesses must register when taxable supplies and imports exceeded AED
  375,000 in the previous 12 months or are expected to exceed it in the next 30 days.
  Voluntary registration may be available above AED 187,500, including qualifying
  taxable expenses. Different rules can apply to non-residents.
- VAT returns and payment are generally due within 28 days after the end of the assigned
  tax period. Tax periods are assigned by the FTA; do not assume every registrant files
  quarterly. Output tax, recoverable input tax, reverse charge, imports, adjustments,
  credit notes, bad-debt relief, and apportionment require source-document support.
- A valid tax invoice and business-use/recovery conditions generally matter for input
  tax. Keep required tax records for the statutory period; special categories such as
  real estate can have longer requirements. Never invent a TRN, tax invoice detail,
  tax treatment, or recoverability conclusion.

CORPORATE TAX
- UAE Corporate Tax applies to tax periods beginning on or after 1 June 2023. The
  starting point is accounting profit or loss, adjusted under the Corporate Tax Law.
- For ordinary taxable persons, the first AED 375,000 of taxable income is taxed at 0%
  and the portion above AED 375,000 at 9%. Do not apply this simplified statement to a
  Qualifying Free Zone Person, Pillar Two/top-up-tax case, exempt person, or another
  special regime without checking the applicable rules.
- UAE juridical persons, Free Zone Persons, and other taxable persons generally must
  register. A natural person is within scope when conducting a UAE business/business
  activity and turnover exceeds AED 1 million in a Gregorian calendar year; wages,
  personal investment income, and qualifying real-estate investment income are excluded
  from that business-activity test.
- A Qualifying Free Zone Person can benefit from 0% on Qualifying Income only if all
  statutory conditions continue to be met. Free Zone status by itself does not make all
  income tax-free.
- Small Business Relief can be elected by an eligible Resident Person when revenue in
  the relevant and prior tax periods does not exceed AED 3 million, subject to exclusions
  and anti-fragmentation rules. Under the currently published decision it applies to tax
  periods ending on or before 31 December 2026; verify any extension for later periods.
- Common adjustment areas include exempt income/participation exemption, non-deductible
  expenditure, 50% client entertainment limitation where applicable, interest limitation,
  related-party/connected-person arm's-length rules, tax losses, foreign tax credits,
  transfer pricing, and reliefs. Facts and documentation control the result.
- Corporate Tax returns and payment are generally due within nine months after the end
  of the tax period. Registration deadlines depend on person type, incorporation/licence
  timing, residence, permanent establishment, nexus, and current FTA decisions.

RESPONSE RULES
- Distinguish VAT from Corporate Tax and accounting treatment from tax treatment.
- Ask for entity type, mainland/free-zone status, tax period, registration status,
  transaction facts, location, counterparty, amounts, and available evidence when needed.
- For filing, penalties, eligibility, exemptions, cross-border, free-zone, real-estate,
  related-party, restructuring, or high-value questions: state assumptions, flag that
  rules can change, and direct the user to current FTA/MoF guidance or a UAE tax adviser.
- Never claim the portal calculation is an official return or legal opinion.

OFFICIAL SOURCES
- FTA VAT: https://tax.gov.ae/en/taxes/Vat/vat.topics.aspx
- FTA VAT registration: https://www.tax.gov.ae/en/services/vat.registration.aspx
- FTA Corporate Tax guides: https://www.tax.gov.ae/en/taxes/corporate.tax/corporate.tax.guides.references.aspx
- FTA Corporate Tax registration: https://tax.gov.ae/en/services/corporate.tax.registration.aspx
- MoF Corporate Tax: https://mof.gov.ae/corporate-tax/
- FTA Corporate Tax legislation: https://tax.gov.ae/en/legislation/corporate.tax.aspx
"""
