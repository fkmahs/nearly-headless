# Design references

What builders read when they make a page: an internal tool page (a form, a table, a review or approval screen, a page served inside a business system, often server-rendered HTML and CSS with little JavaScript), a dashboard or command center like `nh serve`, or a small desktop or artifact app like [answer-deck](../apps/answer-deck/README.md).

Every reference below is actively maintained, openly licensed, well documented and useful for dense business screens: tables, forms, status, empty and error states. Link to them and use what they teach; don't copy their text or assets into a repo. Checked in November 2026.

## Use these

| Reference | Use it for | Licence |
|---|---|---|
| [WCAG 2.2](https://www.w3.org/TR/WCAG22/) ([quick reference](https://www.w3.org/WAI/WCAG22/quickref/)) | The accessibility bar every page meets, at level AA: contrast, keyboard use, focus, labels, error messages, target size. | W3C Document License |
| [WAI-ARIA Authoring Practices](https://www.w3.org/WAI/ARIA/apg/) | How a custom control must behave by keyboard and screen reader, with working examples: dialog, tabs, disclosure, combobox, menu button, sortable table, grid. Read it before building any widget that isn't a plain HTML element. | W3C Software and Document License |
| [MDN Web Docs](https://developer.mozilla.org/) | The native elements that need no library: `<dialog>`, `<details>`, popover, `<table>` markup, built-in form validation, `position: sticky`, `prefers-color-scheme`. First stop for server-rendered pages. | prose CC BY-SA 2.5, code samples CC0 |
| [GOV.UK Design System](https://design-system.service.gov.uk/) | Forms and review screens: one question per page, error summary and inline error messages, summary list for "check your answers" before submit, task list, tables, notification banners. Built to work without JavaScript, so it suits server-rendered pages best of all. | code MIT ([GOV.UK Frontend](https://github.com/alphagov/govuk-frontend)), docs Open Government Licence v3 |
| [U.S. Web Design System](https://designsystem.digital.gov/) | A second plain, accessible take on forms, tables, alerts, step indicators and validation; its [data visualizations](https://designsystem.digital.gov/components/data-visualizations/) page is a short, practical guide to accessible charts (a data table beside each chart, no meaning in colour or hover alone). | public domain (CC0), a few bundled fonts and icons under their own licences |
| [Carbon Design System](https://carbondesignsystem.com/) | The densest enterprise patterns: [data table](https://carbondesignsystem.com/components/data-table/usage/) (toolbar, sorting, row selection and batch actions, expandable rows), notifications, empty states, loading, and [data visualization](https://carbondesignsystem.com/data-visualization/getting-started/) guidance covering chart choice, colour palettes and dashboards. | Apache 2.0 |
| [Primer](https://primer.style/product/ui-patterns/) | Patterns for tools that people use all day: empty states, loading, saving, notification messaging, degraded experiences, forms, progressive disclosure. Closest in tone to `nh serve`. | MIT |

## Motion and animation

Start with CSS transitions and the browser's own [Web Animations API](https://developer.mozilla.org/en-US/docs/Web/API/Web_Animations_API): they cover fades, slides and a card moving to the back of a stack with no library. Reach for a library only when a page needs springs, gestures or sequences CSS can't express.

| Reference | Use it for | Licence |
|---|---|---|
| [Motion](https://motion.dev/) ([repo](https://github.com/motiondivision/motion)) | The one animation library to use when CSS isn't enough: spring physics, drag and swipe gestures, layout changes and staggered lists, for plain JavaScript or React. Small, built on the browser's own animation engine, and very actively maintained. Fits card stacks like answer-deck's and panels that open or reorder on a dashboard. | MIT |

## House rules

Each one is already how the kit's own pages work ([`wk/page.html`](../wk/page.html), [`apps/answer-deck/answer-deck.html`](../apps/answer-deck/answer-deck.html)); copy from them.

1. **One self-contained HTML file, colours as tokens.** Colours live as CSS variables on `:root`, with a dark set under `@media (prefers-color-scheme: dark)`. Text meets WCAG AA contrast (4.5:1, or 3:1 for large text and for borders and icons that carry meaning) in both.
2. **Visible focus, real keyboard use.** Use `<button>` and `<a>`; anything else that acts as one gets `role="button"`, `tabindex="0"` and Enter and Space. Focus shows as `:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px }`.
3. **Status is words plus colour.** A state shows as a labelled pill (waiting, building, ready, failed), never colour alone. A message that appears on its own (loading, saved, failed) sits in a `role="status"` region so screen readers announce it.
4. **Tables stay readable.** Numbers use `font-variant-numeric: tabular-nums` and align right; a wide table scrolls inside its own box, with its header row or first column `position: sticky`.
5. **Respect reduced motion.** Under `@media (prefers-reduced-motion: reduce)` animations and transitions stop (WCAG 2.3.3); check the same setting before running a Motion animation. Nothing a person needs depends on seeing the animation.
6. **Works at phone width.** The layout collapses to one column on narrow screens, with no sideways page scroll.

## Considered, left out

- **Shopify Polaris:** its licence limits use to apps that work with Shopify, and its React library is archived.
- **Atlassian Design System:** free to use only within limits set by Atlassian's own licence; its styles and icons are tied to Atlassian products.
- **Microsoft Fluent 2:** good and MIT-licensed, but its guidance assumes its React components and Microsoft's look; Carbon and Primer cover the same ground for plain pages.
- **SAP Fiori guidelines:** strong business-app patterns, but the guidelines are not openly licensed and its open web components need JavaScript for every control.
- **Material Design:** well documented, but tuned to consumer and mobile apps rather than dense tables and forms.
- **Pico CSS:** was the best classless CSS library; archived in October 2026, no further releases.
- **Water.css:** no changes since early 2024.
- **Simple.css:** maintained, but made for text pages and blogs, not dense tables and forms; a page's own tokens do better.
- **Government Analysis Function chart guidance (UK):** excellent advice on charts, but no open licence is stated; USWDS and Carbon cover the same points.
- **Refactoring UI, Nielsen Norman Group, Inclusive Components, Laws of UX:** good reading, but paid or not openly licensed.
- **Anime.js:** a good MIT animation library, but it overlaps Motion; one library is enough.
- **Lottie (lottie-web):** plays designer-made After Effects animations; our pages have none, and the web player has had no changes since September 2025.
- **Ola, Shifty:** tiny libraries that tween numbers; CSS or Motion already does this.
- **TweenJS:** no commits since 2021.
- **Two.js, Konva:** 2D drawing and canvas editors; the kit draws diagrams as inline SVG.
- **Proton:** decorative particle effects, which business pages don't need.
- **gifenc:** encodes GIF files; not page animation.
- **Remotion, Motion Canvas, Manim:** make videos and explainer animations, not page UI; Remotion also needs a paid licence for most companies.
- **three.js, Babylon.js, Lume:** 3D in the browser; out of scope for business pages.
- **Godot, Blender:** a game engine and a 3D creation suite, not web page tools.
