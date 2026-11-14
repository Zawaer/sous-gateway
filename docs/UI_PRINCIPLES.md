# UI principles for Sous

These principles apply to the seller UI (`/ui/`), the buyer UI (`/buyer/`), `agent.html` and `sign-mandate.html`.

**Who uses the UI.** We design for three people:
- a head of catering who approves a held order on a phone between lunch and dinner service;
- a hotel F&B manager who sets the mandate;
- Valio Aimo sales and IT staff who read the audit trail.

**Where the rules are.** Shared tokens are in [`web/tokens.css`](../web/tokens.css). Screenshots and the review are in [`docs/ui-review/`](ui-review/).

## 1. One decision per screen, decided in five seconds

- **Put the decision first on a phone.** A held order opens with three things, in this order:
  1. the amount;
  2. the reason in one sentence;
  3. the deadline as a clock time ("Decide by Sun 15.11 15:00").

  Approve and Reject follow, in a sticky bottom bar (`.actionbar`). The timeline, ids and signatures come after the buttons.
  - The GOV.UK *check answers* pattern says to show what is being committed to before the button: <https://design-system.service.gov.uk/patterns/check-answers/>.
  - Carbon's dialog pattern says to describe the consequence before the button: <https://carbondesignsystem.com/patterns/dialog-pattern/>.
- **Name the action and the money on the button**, e.g. "Approve €85.20" rather than "OK" or "Submit". Disable the button while the request is running (Carbon dialog pattern).
- **Keep one primary button per view.** Reject is the secondary button. Revoke and other destructive actions ask for confirmation and say what they undo.

## 2. Tiers can't depend on colour

The four mandate outcomes are the core of the product. Each tier has its own **fill weight and icon**, as well as its own colour. WCAG 1.4.1 forbids using colour alone, and Carbon's status-indicator pattern requires shape, icon and text: <https://carbondesignsystem.com/patterns/status-indicator-pattern/>.

| Tier | Label (same on every page) | Badge |
|---|---|---|
| silent | Auto | green outline, ✓ |
| notify | Auto + FYI | blue tint, *i* |
| ask | Needs approval | Valio Aimo yellow fill, dark ink, ‖ |
| block | Refused | red fill, white ink, ✕ |

- **Why the old palette failed.** We simulated it with the Machado 2009 matrices. Under deuteranopia, the old inks for silent (#2e6b3c), ask (#9b6200) and block (#a8321f) all turn olive (#635c3f, #827304, #716519). With the new badges, the fill weights (outline, tint, bright fill, dark fill) still differ for colour-blind users.
- **Use one vocabulary.** Labels are adjectives or past participles ("Refused", not "Refuse"), in sentence case, and say the same thing everywhere:
  - GOV.UK Tag: <https://design-system.service.gov.uk/components/tag/>
  - Polaris Badge: <https://shopify.dev/docs/api/app-home/polaris-web-components/feedback-and-status-indicators/badge>
  - Atlassian Lozenge: <https://atlassian.design/components/lozenge/usage>
- **Tier colours mean tiers only.** "Bearer token required" or "Active" is not a tier, so it gets plain text or a neutral tag.

## 3. Plain language for a Finnish kitchen

- **Write the reason as a sentence with the product name.** For example: "Bravo Chef chopped tomatoes have no allergen data, so we can't confirm they're free of milk and nuts." The rule id (`allergen.unknown`) sits underneath in small mono for Valio Aimo IT.
- **Show what the API sends in plain words.**
  - Write `ST` as "pcs" (Finnish "kpl").
  - Don't show event keys such as `sandbox.created`.
  - Don't show product names in ALL CAPS. They come from the catalog; convert them to sentence case for display.
- **Format money one way.** On a page in Finnish locale, write "1 560,00 €"; in English, write "€1,560.00". Never mix the two in one line, as the engine messages currently do.
- **Write dates like Finnish webshops do.** Use the weekday and date ("ma 16.11.") and a 24-hour time (15:00).
- **Avoid "AI-powered", emoji and vague claims.** Say what code checked: "Checked by the mandate rules. No AI in this step."
- **Match Valio Aimo's tone.** It is informal Finnish *sinä* ("Tule asiakkaaksi") and friendly English, so address the user as "you".

## 4. Credible, not generic

- **Use what we already have.** That means warm paper, black ink, Newsreader headings, Inter for text and Fragment Mono for ids. Add no gradients, glass effects, drop shadows or decorative illustrations.
- **Use Valio Aimo petrol (#005670) only as an accent** for links and focus rings. It is not a tier colour, and it is not a logo we're entitled to use.
- **Back every number with its source.** Each number on Stats or Today links to the audit chain or `evals/results.json`. Hide a "0" bar entirely rather than drawing a stub.
- **Make empty states useful.** They need a short title, one next step and a link, e.g. "Nothing waiting. Start a sandbox to try it: POST /sandbox". Carbon: <https://carbondesignsystem.com/patterns/empty-states-pattern/>.
- **Errors say what happened and what to do.** Don't write "invalid", "oops" or a bare code. Keep what the user typed. GOV.UK: <https://design-system.service.gov.uk/components/error-message/>.

## 5. Accessibility minimums

- **Contrast.** Text needs at least 4.5:1, and badges, borders and icons need at least 3:1 (WCAG 1.4.3 and 1.4.11).
  - `--ink-60` is the lightest token allowed for text.
  - The old `rgba(0,0,0,.55)` passes, but only just (4.66:1 on paper), so don't go lower. `--ink-40` (2.8:1) fails and is for decoration only.
- **Touch targets** are at least 44×44 px on a phone (Apple HIG). WCAG 2.5.8 sets the floor at 24 px. Leave 8 px between neighbouring targets.
- **Keep the visible focus ring** (`--focus-ring`). Labels belong on the field, not in the placeholder.
- **Audit entries answer who, what, which object, when and why.** Entries are immutable and newest first, with absolute times. Rows run 40 px on desktop and 48 px on touch screens (Carbon data table: <https://carbondesignsystem.com/components/data-table/usage/>).
