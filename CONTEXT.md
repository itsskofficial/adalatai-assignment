# Invoice Collection

Collects the billing documents that SaaS vendors email to a company's mailboxes each month, files them as PDFs, and reports what was found and what is absent.

## Language

### Documents

**Billing Document**:
A document recording one movement of money between the company and a vendor. It is an invoice, a receipt or a credit note.
_Avoid_: Bill, statement

**Invoice**:
A billing document in which a vendor requests payment.

**Receipt**:
A billing document confirming a payment was taken. For vendors that issue no invoice, the receipt is the billing document for that charge.

**Credit Note**:
A billing document recording money returned by a vendor. Its amount is negative.
_Avoid_: Refund

**Charge**:
One movement of money. An invoice and a receipt for the same charge are one charge, not two.
_Avoid_: Transaction, payment

**Billing Signal**:
An email about billing that records no movement of money, such as a payment-failed notice or a renewal reminder. It is never collected, but it can explain a gap.
_Avoid_: Notification, alert

**Invoice Format**:
How a billing document arrives: as a PDF attachment, in the email body, or behind a portal link.

**Portal Link**:
A link in an email to a vendor's billing page. It is tokenised if it opens without signing in, and login-gated otherwise.

### Parties and places

**Vendor**:
A SaaS company that bills the company.
_Avoid_: Supplier, provider, tool

**Expected Vendor**:
A vendor the company has said should send a billing document in a given month, according to its billing cycle.

**Suggested Vendor**:
A vendor seen in past months that is not yet an expected vendor, awaiting a person's decision.

**Billing Cycle**:
How often a vendor bills: monthly, or annually in a named renewal month.

**Source Account**:
A mailbox the tool reads. One billing document may be found in several source accounts.
_Avoid_: Inbox, Gmail account, user

### Time

**Invoice Date**:
The date printed on a billing document. It decides the collection month.

**Collection Month**:
The calendar month a run collects for. A billing document belongs to it when its invoice date falls within it, whenever the email arrived.
_Avoid_: Billing period, billing month

### Outcomes

**Run**:
One collection for one collection month across all source accounts.

**Ledger**:
The record of every email examined and every billing document produced, linking each PDF to its source email.

**Collected**:
The state of an email whose billing document was filed and reported.

**Needs Review**:
The state of an email whose extraction a person must confirm before it is reported.

**Skipped**:
The state of an email judged not to be a billing document.

**Failed**:
The state of an email that could not be processed, with a recorded reason.

**Gap**:
An expected vendor with no billing document in the collection month. It is missing when every source account synced, and unknown when one did not. A billing document from the vendor that needs review is not yet in the month, so the gap remains, explained as held for review; a billing signal can explain a gap in the same way.

**Anomaly**:
A billing document whose amount or currency departs from what that vendor usually bills.

**Digest**:
The short message sent after a run, giving what was collected, the gaps, and what needs review.
_Avoid_: Notification, alert, report

**Owner Account**:
The source account that also owns the archive folder and the summary.

**Assisted Download**:
A billing document a person fetched from a login-gated portal and handed to the tool to file.

**Audit Trail**:
The history of one billing document, in order of time: the emails it arrived in, how it was classified, found or uploaded, read, matched to an expected vendor and checked, each retry, whether it was held or collected, each correction and decision by a person, where it was filed, its rate to rupees, and the removal of its pending copy.
_Avoid_: Log, audit log

**Summary**:
The read-only report of a run, one row per charge.
_Avoid_: Summary sheet, spreadsheet, report
