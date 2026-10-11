---
name: dashboard
description: "Load this when somebody asks about their crewmate's Dashboard tab: a page that shows something particular, a change to the one they have, or going back. You write the page and you own the flow -- ASK, save, revert. Every number comes from a fold, never from your typing."
triggers: dashboard, my dashboard, dashboard tab, change my dashboard, another dashboard, dashboard page, show me a different, go back to the old dashboard, build a dashboard, dashboard gallery, dashboard template
---

# The Dashboard

ONE page per crewmate. You write it, the person owns it, and the gateway keeps
it fed.

Three parts, and knowing which is which is most of the job:

| Part | Who | Rule |
|---|---|---|
| the page | you write the HTML | changes only when you save it, and only you can |
| the values | the crew log folds | pushed in as they move, never typed by you |
| the frame | the gateway | no network, text-only bindings, nothing you write runs outside it |

## 1. You own it, and nothing lands unasked

**Ask before you save.** Saving IS landing: a page you wrote has no staged copy
waiting unseen somewhere, so the question comes first, not after.

1. **Write the package.** `view.html` carries your markup, your CSS and your
   script. `model` names every field you bind, and it is also the list the
   controller pushes, so a binding the model does not declare is refused at the
   save and told to you by name.
2. **ASK.** One sentence they can answer, about what you are about to change. A
   page nobody agreed to is a page they undo instead of keep.
3. **Save.** `artifact_save` with `kind="dashboard"`, or `artifact_update` on
   the slug you already own. The store validates the package and cuts a version
   on the layout change itself; pushing data never cuts one.
4. **Say what changed.** Their Dashboard tab is now the page, so describe the
   change, not the contents. They are looking at the contents.

Going back is `artifact_revert` to an earlier version of your own page. A
crewmate still on a catalogue page has `dashboard_rollback(to_version)` instead,
with `dashboard_fields` listing the versions it can still reach -- read that
list rather than guessing, because the store keeps fewer payloads than the
history has rows. Either way going back moves FORWARD: restoring version 1 over
version 2 writes version 3, so it is itself undoable.

Only the owner saves the page or writes its values. A worker is refused on all
three artifact write paths and told to `work_report` instead; the ledger fold
then carries its report onto the page by itself.

Three things not to do:

- Do not save a page they have not agreed to.
- Do not report what the page shows. They are looking at it. Say what changed.
- Do not write a number into the markup. Bind it.

## 2. The design is yours; the honesty is not optional

Choose the information architecture and the visual form the work needs, and
change it when the work changes. A timeline, a dependency map, an experiment
comparison, a release board, a deliverable gallery: examples, not required
panels and not a template.

What is fixed is what the page is allowed to claim:

- **Real evidence only.** Real states and real counts. No invented percentage,
  no made-up elapsed time, no fabricated approval, no "complete" from an idle
  worker.
- **A projection, not a second store.** The page presents what the ledger and
  the folds already hold. It is never the place a status lives.
- **One page, updated in place.** Change the page the person has; do not mint a
  new one per milestone.
- **Numbers come from bindings.** A number you type is true once, at the moment
  you typed it, and wrong every time the page is opened afterwards. A number
  read from a fold is true tomorrow.
- **No imitation controls.** Decisions go through the real `ask_question`
  surface and the runtime's own approval flow. A button you draw in HTML cannot
  change anybody's permissions, and a page that implies it can is lying about
  authority it does not have.
- **Say which claims are yours.** A value you asserted and a value the log
  recorded are different kinds of claim. The frame marks yours; do not blur it.

## 3. The manager view: five questions on one screen

A manager opens this page to know where the work stands. They do not care how
the system works.

| # | Question | Where the answer comes from |
|---|---|---|
| 1 | Is it done? | a count out of a total, from the ledger fold |
| 2 | What did it cost? | the usage fold |
| 3 | What is left? | open items, from the ledger fold |
| 4 | What is stuck? | blocked items and never-ran lanes |
| 5 | What needs me? | anything waiting on a person, FIRST and in a warm colour |

Hard limits, and they are limits rather than advice:

- One screen at 1280x800 with no scroll.
- Needs-you first whenever anything waits on a person; with nothing waiting,
  say "Nothing right now" rather than hiding the block.
- A headline of one line, at most ten words.
- No sentences inside a block. Numbers over words.
- Detail on demand, folded in `details`, not spread across the screen.

Write the vocabulary down before you draw: what one item is, what done means,
what waiting means. A page whose words drift from the work reads as wrong even
when every number is right.

## 4. The frame contract

Your page is a body fragment in a frame the gateway owns. It may run JS.

Three ways a value reaches it, and you choose per element:

1. `data-dashboard-field="<name>"` on an element: the host sets that element's
   **text**. No markup, no attributes -- a text node, so a value can never
   become an element. This is the path for every cell a reader reads.
2. `window.kirocrew.fields` in a script: the same values as data, for a chart to
   draw from. It also carries `agentic` (the values you wrote rather than the
   gateway folded), `seq` (the fold watermark) and `stale`.
3. `window.kirocrew.subscribe(name, cb)`: `cb(value, name)` runs once when you
   subscribe and again on every push that carries that field, and it returns a
   disposer. This is the path for anything you DRAW -- a tree, a chart, a scene.

The host fills (1) itself from the same object it hands to (2) and (3), so a
page cannot show one number in a cell and a different one in its chart.

Where your own script builds rows, create elements and set `textContent`. A
value out of the crew log is whatever somebody wrote there, so assigning a
template string to `innerHTML` is how a recorded value becomes markup on your
page. The bindings in (1) cannot do that to you; your own markup can.

**Hearing an update.** Subscribe during parse and keep drawing: the callback
fires immediately against the read the page already holds, so first paint has
the numbers, and the registry outlives each refill even though
`window.kirocrew` is replaced wholesale. A field counts as present or absent,
not truthy, so a counter reaching `0` still reaches your callback. A throw in
one callback costs only that callback, not the others on the page. There is
also a plain `kirocrew-dashboard` event on `window`, but it only says that
something landed and leaves you to diff a whole read to find out what;
`subscribe` is the better entry point. Bindings update whether or not you
subscribe, so subscribe only for what you draw.

What the frame refuses:

- **No egress.** `connect-src 'none'`. No fetch, no XHR, no websocket, no remote
  font, no beacon. Publish fresh content through the tool instead.
- **No dynamic exec.** The frame mounts with `allow-scripts` and nothing else,
  on an opaque origin with no access to the dashboard's cookies, storage or DOM.
- **A fixed library allowlist**, and it can be used for nothing else, because
  nothing can be sent to it.

Egress is closed harder here than a chat card needs, and the reason is what this
page HOLDS: real numbers out of the operator's own crew log. A page that can both
read those and open a channel is an exfiltration path with a data source
attached.

If your page needs a script to draw itself, carry a `<noscript>` fallback saying
so, and ship anything a script fills HIDDEN so the script reveals it.
`allow-scripts` is withheld wherever an admin ceiling restricts the owner
agent's network egress, and a page that shows a placeholder or an empty-state
line in that mode states something about the owner's data that is not true.

**Staleness.** A field whose source did not resolve is reported, not guessed at.
The page keeps the last values that did resolve and the host shows a band saying
the data is stale. A half-filled page is the one state a status surface must
never reach, because a reader cannot tell it from a current one.

## 5. Which values exist

Fold names available today: `status`, `usage`, `timeline`, `tools`, `approvals`,
`subagents`, `class`, `outline`, `ledger`, `radar`, `work`, `panel`, `agentic`,
`mistakes`, `workstreams`, and a tree-keyed `worktree`.

Each is a list of paths, and a path is dotted keys only -- there is no index
syntax, so a per-row value has no path and a list's LENGTH is what you bind.

Read the live catalogue rather than this paragraph: the `dashboard-template`
skill's `FOLDS.md` and `folds.json` are generated from the product's own
projections, so they are the list that is true today, and `dashboard_fields`
tells you which fields the page you are on declares, which of them are yours,
and which of your past writes were refused. Read it before you write.

Every fold here is available. None is required. A page that needs two of them is
a correct page.

## 6. Theme and reach

The `widgets` skill owns the theme contract and this page follows it: theme
variables, never hard-coded colour, so the page tracks light and dark without a
second design.

- Readable at 320px wide, through to an expanded view. No fixed-width canvas and
  no table that forces the whole page to scroll sideways; stack or disclose dense
  content on a phone.
- Motion off under `prefers-reduced-motion`.
- Colour is never the only carrier of a state: pair it with a word.
- Every link gets `target="_blank" rel="noopener noreferrer"`.
- Plain-language names. A session or run id belongs in folded detail, never as a
  main label.

## 7. Pages to start from

`gallery/` beside this file holds whole working packages. Copy one, change its
`bound_to`, change what it binds, and save it; nothing loads a gallery page by
id, so editing your copy cannot reach anybody else's.

`GALLERY.md`, also beside this file, lists those packages and the catalogue's
pages together, with the reader question each one answers. Copy one and adapt
it, mix two, or write something none of them describes. They are source to
read, not pages to pick.

One rule a copied package will teach you the hard way: the package's `model` is
the list of fields the controller pushes, so a name only your markup knows is a
cell nothing ever fills, and the save is refused and says so. Declare every
field you bind.
