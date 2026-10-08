// Entity types, the groups charts colour them by, and the colours themselves.
// 19 entity types are too many colours to tell apart, so charts colour by group. Series colours
// are assigned in a fixed order (checked for colour-blind separation on the card surface); the
// four status colours are used only for ratings and states, always with a label.

export const SERIES = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181"];
export const NEUTRAL = "#5d6677";
export const ACCENT = SERIES[0];
export const RAMP = ["#17243a", "#184f95", "#3987e5", "#9ec5f4"]; // one hue: little -> much

export const GROUPS: Record<string, string[]> = {
  People: ["PERSON", "DATE_OF_BIRTH", "HEALTH_DATA"],
  Contact: ["EMAIL_ADDRESS", "PHONE_NUMBER", "ADDRESS"],
  "Government & financial ID": ["US_SSN", "PASSPORT", "IN_PAN", "PL_PESEL", "TAX_ID", "NATIONAL_ID", "IN_AADHAAR",
    "CREDIT_CARD", "IBAN_CODE", "BANK_ACCOUNT", "UPI_ID", "DRIVING_LICENCE", "IN_VOTER_ID", "UK_NINO"],
  "Internal ID": ["EMPLOYEE_ID", "VENDOR_ID"],
  "Secrets & network": ["CREDENTIAL", "IP_ADDRESS", "CONFIDENTIAL_TERM"],
};
export const OTHER = "Other / unreadable";
export const GROUP_ORDER = [...Object.keys(GROUPS), OTHER];
export const GROUP_COLOR: Record<string, string> = Object.fromEntries([
  ...Object.keys(GROUPS).map((g, i) => [g, SERIES[i]]),
  [OTHER, NEUTRAL],
]);

const GROUP_OF: Record<string, string> = {};
for (const [g, entities] of Object.entries(GROUPS)) for (const e of entities) GROUP_OF[e] = g;

export const groupOf = (entity: string) => GROUP_OF[entity] ?? OTHER;
export const entityColor = (entity: string) => GROUP_COLOR[groupOf(entity)];

const LABEL: Record<string, string> = {
  EMAIL_ADDRESS: "E-mail address", US_SSN: "US SSN", IN_PAN: "PAN (India)", PL_PESEL: "PESEL (Poland)",
  IN_AADHAAR: "Aadhaar (India)", TAX_ID: "Tax ID", NATIONAL_ID: "National ID", EMPLOYEE_ID: "Employee ID",
  VENDOR_ID: "Vendor ID", IBAN_CODE: "IBAN", IP_ADDRESS: "IP address", LOW_CONFIDENCE_OCR: "Unreadable OCR text",
  BANK_ACCOUNT: "Bank account", UPI_ID: "UPI ID (India)", DRIVING_LICENCE: "Driving licence", IN_VOTER_ID: "Voter ID (India)",
  UK_NINO: "National Insurance no. (UK)", HEALTH_DATA: "Health data", CONFIDENTIAL_TERM: "Confidential term",
  CREDENTIAL: "Credential",
};

/** US_SSN -> US SSN, DATE_OF_BIRTH -> Date of birth */
export function pretty(entity: string): string {
  if (LABEL[entity]) return LABEL[entity];
  const s = entity.replace(/_/g, " ").toLowerCase();
  return s.charAt(0).toUpperCase() + s.slice(1);
}

export const DECISION_LABEL: Record<string, string> = {
  redact: "Auto-redacted", review: "Redacted + review", drop: "Dropped",
};
export const DECISION_COLOR: Record<string, string> = { redact: SERIES[0], review: SERIES[1], drop: NEUTRAL };

export type Status = "good" | "warning" | "serious" | "critical" | "none";
export const RATING_ORDER = ["critical", "high", "medium", "low", "none"];
export const RATING_STATUS: Record<string, Status> = {
  critical: "critical", high: "serious", medium: "warning", low: "good", none: "none",
};
export const STATUS_COLOR: Record<Status, string> = {
  good: "#0ca30c", warning: "#fab219", serious: "#ec835a", critical: "#d03b3b", none: NEUTRAL,
};
export const RATING_ICON: Record<string, string> = { critical: "▲", high: "▲", medium: "◆", low: "●", none: "○" };
export const ratingColor = (rating: string) => STATUS_COLOR[RATING_STATUS[rating] ?? "none"];

export const CONTEXT_ORDER = ["table", "labelled", "narrative", "image", "metadata"];
