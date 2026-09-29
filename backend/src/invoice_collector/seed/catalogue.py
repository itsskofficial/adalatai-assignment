"""The vendors that bill the fictional company, and the other mail they send."""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

InvoiceFormat = Literal["attachment", "body", "portal_link"]
PortalKind = Literal["tokenised", "login_gated"]
BillingCycle = Literal["monthly", "annual"]
DateStyle = Literal["long", "short", "iso"]
# Position of a source account in the configured three.
Engineering, Ops, Finance = 0, 1, 2

COMPANY = "Nyaya Labs Pvt Ltd"
COMPANY_ADDRESS = "4th Floor, 80 Feet Road, Indiranagar, Bengaluru 560038, India"
DEFAULT_SOURCE_ACCOUNTS = (
    "engineering@nyayalabs.example",
    "ops@nyayalabs.example",
    "finance@nyayalabs.example",
)
DEFAULT_CURRENCY = "USD"

CENT = Decimal("0.01")


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Line:
    description: str
    quantity: int
    unit_price: Decimal
    # Usage is metered, so its quantity moves a little from month to month.
    usage: bool = False


@dataclass(frozen=True)
class Vendor:
    name: str
    legal_name: str
    address: str
    sender_name: str
    sender_address: str
    account: int
    invoice_format: InvoiceFormat
    document_type: Literal["invoice", "receipt"]
    currency: str
    billing_day: int
    subject: str
    number_prefix: str
    lines: tuple[Line, ...]
    portal: PortalKind | None = None
    billing_cycle: BillingCycle = "monthly"
    renewal_month: int | None = None
    tax_label: str = "Tax"
    tax_rate: Decimal = Decimal(0)
    date_style: DateStyle = "long"
    # Billed on the last day of the month, emailed early the next morning.
    emailed_next_day: bool = False
    expected: bool = True

    @property
    def slug(self) -> str:
        return "".join(c for c in self.name.lower().replace(" ", "-") if c.isalnum() or c == "-")

    @property
    def sender(self) -> str:
        return f"{self.sender_name} <{self.sender_address}>"

    @property
    def usual_amount(self) -> Decimal:
        subtotal = sum((money(line.quantity * line.unit_price) for line in self.lines), Decimal(0))
        return subtotal + money(subtotal * self.tax_rate)


def _d(text: str) -> Decimal:
    return Decimal(text)


_AWS_REGIONS = (
    ("Asia Pacific (Mumbai)", Decimal("1.0")),
    ("Asia Pacific (Singapore)", Decimal("0.4")),
    ("US East (N. Virginia)", Decimal("0.2")),
)
_AWS_SERVICES = (
    ("Amazon EC2, m6i.xlarge On-Demand, instance hours", 1440, "0.2020"),
    ("Amazon EC2, c6g.large On-Demand, instance hours", 2160, "0.0680"),
    ("Amazon EC2, EBS gp3 storage, GB-months", 1800, "0.0912"),
    ("Amazon EC2, data transfer out, GB", 950, "0.1093"),
    ("Amazon RDS for PostgreSQL, db.r6g.large, instance hours", 720, "0.2580"),
    ("Amazon RDS, gp3 storage, GB-months", 500, "0.1310"),
    ("Amazon S3, Standard storage, GB-months", 4200, "0.0250"),
    ("Amazon S3, PUT, COPY, POST and LIST requests, thousands", 3100, "0.0050"),
    ("Amazon CloudFront, data transfer out, GB", 1300, "0.1090"),
    ("AWS Lambda, requests, millions", 46, "0.2000"),
    ("AWS Lambda, compute, thousand GB-seconds", 820, "0.0167"),
    ("Amazon ElastiCache, cache.t4g.medium, node hours", 1440, "0.0650"),
    ("Amazon SQS, requests, millions", 38, "0.4000"),
    ("Amazon CloudWatch, custom metrics", 410, "0.3000"),
    ("Amazon CloudWatch, logs ingested, GB", 260, "0.6700"),
    ("Elastic Load Balancing, Application Load Balancer hours", 1440, "0.0239"),
)
_AWS_LINES = tuple(
    Line(f"{service}, {region}", max(1, int(quantity * share)), _d(price), usage=True)
    for region, share in _AWS_REGIONS
    for service, quantity, price in _AWS_SERVICES
)

VENDORS: tuple[Vendor, ...] = (
    Vendor(
        name="GitHub",
        legal_name="GitHub, Inc.",
        address="88 Colin P Kelly Jr Street, San Francisco, CA 94107, United States",
        sender_name="GitHub",
        sender_address="noreply@github.com",
        account=Engineering,
        invoice_format="attachment",
        document_type="receipt",
        currency="USD",
        billing_day=5,
        subject="[GitHub] Payment receipt for nyaya-labs",
        number_prefix="GH",
        lines=(
            Line("GitHub Team, seats, monthly", 21, _d("4.00")),
            Line("GitHub Copilot Business, seats, monthly", 12, _d("19.00")),
        ),
        date_style="iso",
    ),
    Vendor(
        name="AWS",
        legal_name="Amazon Web Services, Inc.",
        address="410 Terry Avenue North, Seattle, WA 98109, United States",
        sender_name="Amazon Web Services",
        sender_address="no-reply-aws@amazon.com",
        account=Engineering,
        invoice_format="attachment",
        document_type="invoice",
        currency="USD",
        billing_day=3,
        subject="Amazon Web Services Invoice Available [Account: 447122901183] [ID: {number}]",
        number_prefix="AWS",
        lines=_AWS_LINES,
    ),
    Vendor(
        name="Linear",
        legal_name="Linear Orbit, Inc.",
        address="2261 Market Street #4917, San Francisco, CA 94114, United States",
        sender_name="Linear",
        sender_address="billing@linear.app",
        account=Engineering,
        invoice_format="portal_link",
        portal="tokenised",
        document_type="invoice",
        currency="USD",
        billing_day=12,
        subject="Your Linear invoice is ready",
        number_prefix="LIN",
        lines=(Line("Linear Business plan, seats, monthly", 18, _d("14.00")),),
        date_style="short",
    ),
    Vendor(
        name="Vercel",
        legal_name="Vercel Inc.",
        address="440 N Barranca Avenue #4133, Covina, CA 91723, United States",
        sender_name="Vercel Inc.",
        sender_address="billing@vercel.com",
        account=Engineering,
        invoice_format="attachment",
        document_type="invoice",
        currency="USD",
        billing_day=9,
        subject="Your Vercel invoice {number}",
        number_prefix="VRC",
        lines=(
            Line("Pro plan, team seats, monthly", 6, _d("20.00")),
            Line("Additional bandwidth, GB", 140, _d("0.15"), usage=True),
        ),
    ),
    Vendor(
        name="Datadog",
        legal_name="Datadog, Inc.",
        address="620 8th Avenue, 45th Floor, New York, NY 10018, United States",
        sender_name="Datadog Billing",
        sender_address="billing@datadoghq.com",
        account=Engineering,
        invoice_format="attachment",
        document_type="invoice",
        currency="USD",
        billing_day=17,
        subject="Datadog invoice {number} for Nyaya Labs",
        number_prefix="DD",
        lines=(
            Line("Infrastructure Pro, hosts, on-demand", 20, _d("18.00")),
            Line("APM Pro, hosts, on-demand", 8, _d("36.00")),
            Line("Log Management, ingested GB", 150, _d("0.10"), usage=True),
        ),
    ),
    Vendor(
        name="OpenAI",
        legal_name="OpenAI, LLC",
        address="548 Market Street, PMB 97273, San Francisco, CA 94104, United States",
        sender_name="OpenAI",
        sender_address="noreply@tm.openai.com",
        account=Engineering,
        invoice_format="body",
        document_type="receipt",
        currency="USD",
        billing_day=31,
        emailed_next_day=True,
        subject="Your OpenAI receipt #{number}",
        number_prefix="OAI",
        lines=(
            Line("ChatGPT Team, seats, monthly", 10, _d("30.00")),
            Line("API usage credits", 4, _d("50.00")),
        ),
        date_style="short",
    ),
    Vendor(
        name="Loom",
        legal_name="Loom, Inc.",
        address="140 2nd Street, 3rd Floor, San Francisco, CA 94105, United States",
        sender_name="Loom",
        sender_address="billing@loom.com",
        account=Engineering,
        invoice_format="attachment",
        document_type="receipt",
        currency="USD",
        billing_day=19,
        subject="Your Loom receipt",
        number_prefix="LM",
        lines=(Line("Loom Business, creators, monthly", 5, _d("15.00")),),
        expected=False,
    ),
    Vendor(
        name="Slack",
        legal_name="Slack Technologies Limited",
        address="Salesforce Tower, 60 R801, North Dock, Dublin, Ireland",
        sender_name="Slack",
        sender_address="feedback@slack.com",
        account=Ops,
        invoice_format="attachment",
        document_type="invoice",
        currency="USD",
        billing_day=3,
        subject="Your Slack invoice is available",
        number_prefix="SBIE",
        lines=(
            Line("Business+ plan, members, monthly", 42, _d("15.00")),
            Line("Prorated members added last month", 3, _d("7.50")),
        ),
        tax_label="Tax (0%)",
    ),
    Vendor(
        name="Notion",
        legal_name="Notion Labs, Inc.",
        address="2300 Harrison Street, San Francisco, CA 94110, United States",
        sender_name="Notion",
        sender_address="team@mail.notion.so",
        account=Ops,
        invoice_format="body",
        document_type="receipt",
        currency="EUR",
        billing_day=14,
        subject="Your receipt from Notion #{number}",
        number_prefix="NTN",
        lines=(Line("Notion Plus plan, members, monthly", 18, _d("10.00")),),
        tax_label="VAT (23%)",
        tax_rate=_d("0.23"),
        date_style="short",
    ),
    Vendor(
        name="Zoom",
        legal_name="Zoom Video Communications, Inc.",
        address="55 Almaden Boulevard, 6th Floor, San Jose, CA 95113, United States",
        sender_name="Zoom",
        sender_address="billing@zoom.us",
        account=Ops,
        invoice_format="attachment",
        document_type="invoice",
        currency="USD",
        billing_day=20,
        subject="Zoom invoice {number}",
        number_prefix="INV",
        lines=(Line("Zoom Workplace Pro, licences, monthly", 12, _d("15.99")),),
    ),
    Vendor(
        name="Google Workspace",
        legal_name="Google Cloud India Private Limited",
        address="Unit 207, Signature Tower II, Sector 15, Gurugram 122001, India",
        sender_name="Google Payments",
        sender_address="payments-noreply@google.com",
        account=Ops,
        invoice_format="portal_link",
        portal="login_gated",
        document_type="invoice",
        currency="INR",
        billing_day=2,
        subject="Google Workspace: your invoice is available for nyayalabs.example",
        number_prefix="GWS",
        lines=(Line("Google Workspace Business Standard, users, monthly", 45, _d("864.00")),),
        tax_label="IGST (18%)",
        tax_rate=_d("0.18"),
        date_style="short",
    ),
    Vendor(
        name="Atlassian",
        legal_name="Atlassian Pty Ltd",
        address="Level 6, 341 George Street, Sydney NSW 2000, Australia",
        sender_name="Atlassian",
        sender_address="billing@atlassian.com",
        account=Ops,
        invoice_format="attachment",
        document_type="invoice",
        currency="USD",
        billing_day=11,
        subject="Invoice {number} from Atlassian",
        number_prefix="AT",
        lines=(
            Line("Jira Software Standard, users, monthly", 30, _d("8.15")),
            Line("Confluence Standard, users, monthly", 30, _d("6.05")),
        ),
        date_style="short",
    ),
    Vendor(
        name="Calendly",
        legal_name="Calendly LLC",
        address="115 E Main Street, Suite A1B, Buford, GA 30518, United States",
        sender_name="Calendly",
        sender_address="billing@calendly.com",
        account=Ops,
        invoice_format="body",
        document_type="receipt",
        currency="USD",
        billing_day=22,
        subject="Your Calendly receipt",
        number_prefix="CAL",
        lines=(Line("Teams plan, seats, monthly", 8, _d("16.00")),),
    ),
    Vendor(
        name="1Password",
        legal_name="AgileBits Inc. dba 1Password",
        address="4711 Yonge Street, 10th Floor, Toronto, ON M2N 6K8, Canada",
        sender_name="1Password",
        sender_address="billing@1password.com",
        account=Ops,
        invoice_format="attachment",
        document_type="invoice",
        currency="USD",
        billing_day=24,
        billing_cycle="annual",
        renewal_month=9,
        subject="Your 1Password invoice",
        number_prefix="OP",
        lines=(Line("1Password Business, users, annual", 45, _d("95.88")),),
    ),
    Vendor(
        name="Figma",
        legal_name="Figma, Inc.",
        address="760 Market Street, Floor 10, San Francisco, CA 94102, United States",
        sender_name="Figma",
        sender_address="billing@figma.com",
        account=Finance,
        invoice_format="portal_link",
        portal="tokenised",
        document_type="invoice",
        currency="USD",
        billing_day=21,
        subject="Your Figma invoice is ready",
        number_prefix="FIG",
        lines=(
            Line("Professional team, Full seats, monthly", 6, _d("15.00")),
            Line("Dev Mode seats, monthly", 4, _d("25.00")),
        ),
        date_style="iso",
    ),
    Vendor(
        name="Zoho",
        legal_name="Zoho Corporation Private Limited",
        address="Estancia IT Park, Plot 140 and 151, GST Road, Chengalpattu 603202, India",
        sender_name="Zoho",
        sender_address="billing@zohocorp.com",
        account=Finance,
        invoice_format="attachment",
        document_type="invoice",
        currency="INR",
        billing_day=7,
        subject="Invoice {number} from Zoho Corporation",
        number_prefix="ZC",
        lines=(
            Line("Zoho Books Professional, organisation, monthly", 1, _d("2999.00")),
            Line("Zoho Payroll, employees, monthly", 45, _d("50.00")),
        ),
        tax_label="IGST (18%)",
        tax_rate=_d("0.18"),
        date_style="short",
    ),
    Vendor(
        name="HubSpot",
        legal_name="HubSpot Ireland Limited",
        address="1 Sir John Rogerson's Quay, Dublin 2, Ireland",
        sender_name="HubSpot",
        sender_address="billing@hubspot.com",
        account=Finance,
        invoice_format="attachment",
        document_type="invoice",
        currency="EUR",
        billing_day=15,
        subject="Your HubSpot invoice {number}",
        number_prefix="HS",
        lines=(
            Line("Marketing Hub Starter, seats, monthly", 3, _d("18.00")),
            Line("Sales Hub Professional, seats, monthly", 4, _d("90.00")),
        ),
        tax_label="VAT (0%, reverse charge)",
        date_style="short",
    ),
    Vendor(
        name="Xero",
        legal_name="Xero (UK) Limited",
        address="5th Floor, 100 Avebury Boulevard, Milton Keynes MK9 1FH, United Kingdom",
        sender_name="Xero",
        sender_address="billing@post.xero.com",
        account=Finance,
        invoice_format="body",
        document_type="invoice",
        currency="GBP",
        billing_day=10,
        subject="Your Xero invoice {number}",
        number_prefix="XR",
        lines=(
            Line("Xero Standard plan, monthly", 1, _d("33.00")),
            Line("Payroll add-on, employees, monthly", 10, _d("1.00")),
        ),
        tax_label="VAT (20%)",
        tax_rate=_d("0.20"),
        date_style="short",
    ),
    Vendor(
        name="DocuSign",
        legal_name="DocuSign, Inc.",
        address="221 Main Street, Suite 1550, San Francisco, CA 94105, United States",
        sender_name="DocuSign",
        sender_address="billing@docusign.com",
        account=Finance,
        invoice_format="body",
        document_type="receipt",
        currency="USD",
        billing_day=26,
        subject="Your DocuSign receipt",
        number_prefix="DS",
        lines=(Line("Business Pro, seats, monthly", 5, _d("40.00")),),
    ),
    Vendor(
        name="Canva",
        legal_name="Canva Pty Ltd",
        address="110 Kippax Street, Surry Hills NSW 2010, Australia",
        sender_name="Canva",
        sender_address="no-reply@canva.com",
        account=Finance,
        invoice_format="attachment",
        document_type="receipt",
        currency="USD",
        billing_day=16,
        billing_cycle="annual",
        renewal_month=7,
        subject="Your Canva receipt",
        number_prefix="CNV",
        lines=(Line("Canva Teams, people, annual", 10, _d("100.00")),),
    ),
)


def vendor(name: str) -> Vendor:
    return next(v for v in VENDORS if v.name == name)


NonBillingLabel = Literal[
    "newsletter", "promotion_with_price", "product_update", "security_alert", "usage_alert"
]


@dataclass(frozen=True)
class OtherMail:
    """An email that is neither a billing document nor a billing signal."""

    key: str
    label: NonBillingLabel
    sender_name: str
    sender_address: str
    account: int
    day: int
    subject: str
    paragraphs: tuple[str, ...]
    # Which history months repeat it, counted from the earliest: 0, 1 or both.
    history: tuple[int, ...] = ()

    @property
    def sender(self) -> str:
        return f"{self.sender_name} <{self.sender_address}>"


OTHER_MAIL: tuple[OtherMail, ...] = (
    OtherMail(
        key="github-newsletter",
        label="newsletter",
        sender_name="GitHub",
        sender_address="newsletter@github.com",
        account=Engineering,
        day=6,
        subject="The GitHub Insider: what shipped in {month_name}",
        paragraphs=(
            "Here is what is new on GitHub this month.",
            "Code scanning now suggests fixes for more languages, and merge queues "
            "are available on every plan.",
            "Read the full changelog on the GitHub blog. You are receiving this because "
            "you subscribed to The GitHub Insider.",
        ),
        history=(0, 1),
    ),
    OtherMail(
        key="github-ssh-key",
        label="security_alert",
        sender_name="GitHub",
        sender_address="noreply@github.com",
        account=Engineering,
        day=13,
        subject="[GitHub] A new SSH authentication public key was added to your account",
        paragraphs=(
            "The following SSH key was added to the nyaya-labs-ci account:",
            "deploy-runner-02 SHA256:pT3kqv0Zr1m8sYw2QeLx7nUa5cJd9fGh4iKo6bNs",
            "If you believe this key was added in error, remove the key and review "
            "your security log.",
        ),
    ),
    OtherMail(
        key="linear-changelog",
        label="product_update",
        sender_name="Linear",
        sender_address="changelog@linear.app",
        account=Engineering,
        day=15,
        subject="Linear changelog: triage rules and faster search",
        paragraphs=(
            "New this week in Linear.",
            "Triage rules route incoming issues to the right team automatically. "
            "Search is now twice as fast on large workspaces.",
            "No action is needed. These changes are live in your workspace.",
        ),
    ),
    OtherMail(
        key="datadog-promotion",
        label="promotion_with_price",
        sender_name="Datadog",
        sender_address="marketing@datadoghq.com",
        account=Engineering,
        day=19,
        subject="Save 20% on Cloud SIEM: from $5.00 per million events",
        paragraphs=(
            "Detect threats across your cloud in real time.",
            "For a limited time, Cloud SIEM starts at $5.00 per million events analysed, "
            "down from $6.25. Commit for a year and save a further 20%.",
            "Talk to your account manager to add Cloud SIEM to your plan. This is a "
            "promotional message and nothing has been charged.",
        ),
    ),
    OtherMail(
        key="aws-root-sign-in",
        label="security_alert",
        sender_name="Amazon Web Services",
        sender_address="no-reply@signin.aws",
        account=Engineering,
        day=22,
        subject="AWS account 447122901183: root user sign-in detected",
        paragraphs=(
            "A sign-in to the root user of your AWS account was detected from a new "
            "location: Bengaluru, India.",
            "If this was you, no action is needed. If not, change the root password "
            "and review account activity in CloudTrail.",
        ),
    ),
    OtherMail(
        key="vercel-usage",
        label="usage_alert",
        sender_name="Vercel",
        sender_address="notifications@vercel.com",
        account=Engineering,
        day=24,
        subject="nyaya-labs has used 80% of its included bandwidth",
        paragraphs=(
            "Your team nyaya-labs has used 800 GB of the 1 TB of bandwidth included "
            "in the Pro plan this cycle.",
            "Usage beyond the included amount is charged at $0.15 per GB and will "
            "appear on your next invoice. Nothing has been charged yet.",
        ),
    ),
    OtherMail(
        key="openai-newsletter",
        label="newsletter",
        sender_name="OpenAI",
        sender_address="news@email.openai.com",
        account=Engineering,
        day=27,
        subject="OpenAI developer news for {month_name}",
        paragraphs=(
            "New models, lower latency and a redesigned playground.",
            "Read the developer changelog for migration notes and deprecation dates.",
        ),
    ),
    OtherMail(
        key="notion-newsletter",
        label="newsletter",
        sender_name="Notion",
        sender_address="team@updates.notion.so",
        account=Ops,
        day=4,
        subject="What is new in Notion: {month_name}",
        paragraphs=(
            "Forms, charts and a faster calendar arrived this month.",
            "See how teams use the new layouts to plan their quarter.",
            "You are receiving this because you are an admin of the Nyaya Labs workspace.",
        ),
        history=(0, 1),
    ),
    OtherMail(
        key="zoom-promotion",
        label="promotion_with_price",
        sender_name="Zoom",
        sender_address="offers@zoom.us",
        account=Ops,
        day=8,
        subject="Upgrade to Zoom Workplace Business for $18.33 per user",
        paragraphs=(
            "Get 300 participants, managed domains and company branding.",
            "Upgrade this month for $18.33 per user per month, billed annually. "
            "That is a saving of $36.00 per user each year.",
            "This offer is optional. Your current plan is unchanged.",
        ),
    ),
    OtherMail(
        key="slack-update",
        label="product_update",
        sender_name="Slack",
        sender_address="no-reply@slack.com",
        account=Ops,
        day=10,
        subject="New in Slack: lists, canvases and huddle notes",
        paragraphs=(
            "Your workspace has new features.",
            "Track work with lists, keep decisions in canvases and let huddle notes "
            "write the summary for you.",
        ),
        history=(0, 1),
    ),
    OtherMail(
        key="1password-sign-in",
        label="security_alert",
        sender_name="1Password",
        sender_address="support@1password.com",
        account=Ops,
        day=13,
        subject="New 1Password sign-in from Chrome on Windows",
        paragraphs=(
            "Your 1Password account was used to sign in from a new device.",
            "Device: Chrome on Windows. Location: Bengaluru, India.",
            "If this was not you, change your account password and regenerate your Secret Key.",
        ),
    ),
    OtherMail(
        key="google-security",
        label="security_alert",
        sender_name="Google",
        sender_address="no-reply@accounts.google.com",
        account=Ops,
        day=17,
        subject="Security alert: new sign-in on Mac",
        paragraphs=(
            "We noticed a new sign-in to your Google Account on a Mac device.",
            "If this was you, you do not need to do anything. If not, we will help "
            "you secure your account.",
        ),
    ),
    OtherMail(
        key="calendly-newsletter",
        label="newsletter",
        sender_name="Calendly",
        sender_address="hello@calendly.com",
        account=Ops,
        day=27,
        subject="Five ways to cut scheduling time in half",
        paragraphs=(
            "Round robin routing, meeting polls and shared event types.",
            "Read the guide and watch the recorded webinar.",
        ),
    ),
    OtherMail(
        key="xero-newsletter",
        label="newsletter",
        sender_name="Xero",
        sender_address="news@post.xero.com",
        account=Finance,
        day=4,
        subject="Xero news: bank feeds, reports and more",
        paragraphs=(
            "A round-up of what changed in Xero this month.",
            "New bank feeds are available and the management report pack has a fresh layout.",
        ),
        history=(0, 1),
    ),
    OtherMail(
        key="canva-promotion",
        label="promotion_with_price",
        sender_name="Canva",
        sender_address="marketing@canva.com",
        account=Finance,
        day=6,
        subject="Add Canva Enterprise from $30.00 per person",
        paragraphs=(
            "Brand controls, single sign-on and unlimited storage.",
            "Canva Enterprise starts at $30.00 per person per month. Upgrade before "
            "the end of the month and the first two months are free.",
            "This is an offer. Your plan and your payments are unchanged.",
        ),
        history=(0, 1),
    ),
    OtherMail(
        key="figma-update",
        label="product_update",
        sender_name="Figma",
        sender_address="no-reply@figma.com",
        account=Finance,
        day=12,
        subject="New in Figma: auto layout wraps and variables in prototypes",
        paragraphs=(
            "The latest release is live for your team.",
            "Auto layout can now wrap, and prototypes can read and set variables.",
        ),
        history=(0,),
    ),
    OtherMail(
        key="docusign-security",
        label="security_alert",
        sender_name="DocuSign",
        sender_address="dse@docusign.net",
        account=Finance,
        day=14,
        subject="Your DocuSign password was changed",
        paragraphs=(
            "The password for your DocuSign account was changed.",
            "If you did not make this change, contact your administrator and "
            "reset your password straight away.",
        ),
    ),
    OtherMail(
        key="hubspot-newsletter",
        label="newsletter",
        sender_name="HubSpot",
        sender_address="newsletter@hubspot.com",
        account=Finance,
        day=18,
        subject="The HubSpot customer newsletter",
        paragraphs=(
            "Product news, training and events for HubSpot customers.",
            "Register for the autumn customer conference and browse new courses in the academy.",
        ),
    ),
    OtherMail(
        key="zoho-update",
        label="product_update",
        sender_name="Zoho Books",
        sender_address="noreply@zohobooks.com",
        account=Finance,
        day=20,
        subject="Zoho Books update: e-invoicing changes from October",
        paragraphs=(
            "Changes to e-invoicing rules take effect on 1 October.",
            "Zoho Books will apply the new rules automatically. Review the help "
            "article to see what changes on your sales documents.",
        ),
    ),
    OtherMail(
        key="hubspot-webinar",
        label="promotion_with_price",
        sender_name="HubSpot",
        sender_address="events@hubspot.com",
        account=Finance,
        day=24,
        subject="Early bird tickets for INBOUND: save €200.00",
        paragraphs=(
            "Join thousands of marketing and sales leaders this autumn.",
            "Early bird tickets are €499.00 until the end of the month, a saving "
            "of €200.00 on the full price.",
            "Tickets are sold separately from your HubSpot subscription.",
        ),
    ),
)
