---
status: accepted
---

# The dashboard is styled with Tailwind and components checked in as source

The dashboard's screens are built from Tailwind CSS utilities over a small set of design tokens, and from shadcn-style components on Radix primitives that live in the repository as source files under `frontend/src/components/ui`. Charts are drawn with Recharts. The theme follows the system's light or dark preference until a person chooses one, and that choice is kept in the browser.

This supersedes the earlier choice of a React front end with no UI library and one hand-written stylesheet.

## Why

- **A finance team reads this every month.** Tables that line up, states that read at a glance and a layout that works on a phone are what makes the dashboard usable, and a hand-written stylesheet was growing past the point where every screen looked like the others.
- **Accessible menus, dialogs and tabs are hard to write and easy to get wrong.** Radix gives keyboard reach, focus handling and the right roles for each; the tests find every control by role, name and label, and they did not have to change to find them.
- **The components are ours.** Checked in as source, they are read, changed and reviewed like any other file, and nothing generates them at build time. The build stays `npm ci` and `npm run build`.
- **Nothing is fetched from a third party at runtime.** Fonts are the system's, with Inter used where it is installed. Tailwind runs at build time and ships only the classes the screens use.

## Considered Options

- **Keep the hand-written stylesheet and extend it**: rejected. Each new screen re-decided spacing, colours and states, and the accessible parts (dialogs, menus, tabs) would still have to be written by hand.
- **A component library installed as a dependency (Material UI, Chakra, Mantine)**: rejected. Its look is its own, its bundle is large, and changing a component means fighting it.
- **Tailwind with shadcn-style components as source**: chosen. The look is decided once in tokens, the accessible parts come from Radix, and every component is a file in the repository.
- **A second charting or icon library**: rejected. Recharts draws the charts, lucide the icons, and nothing else is added for either.

## Consequences

- Native `select` and checkbox controls are kept, styled to match, so a person on a phone gets the browser's own control and the tests choose an option as a person does.
- The charting library is loaded only when a chart has a width to be drawn at, so the rest of the dashboard does not carry it.
- Destructive actions (removing a vendor, a person or a source account, rejecting an email) ask in an alert dialog. Three tests that looked for the question inline now look for it in the dialog; what they check is unchanged.
