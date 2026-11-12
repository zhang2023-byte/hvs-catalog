# Issue tracker: GitHub

Issues and specs for this repo live as GitHub issues in `zhang2023-byte/hvs-catalog`.
Use the `gh` CLI. This file covers only the tool workflow. A defect in the published catalog
is an ordinary issue carrying the `data-defect` label, which is also what the website's
"Known issues" link filters on, so readers see only those.

## Conventions

- Create: `gh issue create --title "..." --body "..."` with a heredoc for long bodies.
- Read: `gh issue view <n> --json number,title,body,labels,comments`.
- List: `gh issue list --state open --json number,title,body,labels,comments`.
- Comment: `gh issue comment <n> --body "..."`.
- Label: `gh issue edit <n> --add-label "..."` / `--remove-label "..."`.
- Close: comment the answer, then `gh issue close <n>`.
- Sub-issue of a parent: `gh issue edit <parent> --add-sub-issue <child>` (gh 2.94+).
- Blocking edge: `gh issue edit <child> --add-blocked-by <blocker>`; a ticket is
  unblocked when every blocker is closed.
- Open blockers: `gh api repos/{owner}/{repo}/issues/<n> --jq .issue_dependencies_summary.blocked_by`.

`gh` infers the repo from the clone. Never file into an upstream repository by accident;
confirm `gh repo view --json nameWithOwner` before the first write of a session.

## Pull requests as a request surface

**No.** External PRs are not triaged as feature requests.

## When a skill says "publish to the issue tracker"

Create a GitHub issue.

## When a skill says "fetch the relevant ticket"

Read it as above.

## Wayfinding operations

Used by `/wayfinder`.

- Map: one issue labelled `wayfinder:map` holding Destination, Notes, Decisions so far,
  Not yet specified and Out of scope.
- Ticket: a child issue labelled `wayfinder:<type>` (`research`, `prototype`, `grilling`,
  `task`), sized to one session. Never add a triage label to a wayfinder ticket.
- Claim: `gh issue edit <n> --add-assignee @me` as the first write.
- Frontier: open children with no open blocker and no assignee; first in map order wins.
- Resolve: post the answer as a comment, close the ticket, then append a one-line context
  pointer to the map's Decisions so far.
