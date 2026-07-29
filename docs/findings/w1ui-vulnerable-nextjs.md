# Finding W1-UI-1 — The portal runs a Next.js version with a known vulnerability

- **Severity:** Medium–High (unassessed — see below)
- **Status:** **Named, not fixed.** Needs a decision from Riverbend.
- **Found during:** installing the frontend test toolchain, from npm's own warning

---

## What we saw

Running `npm install` in `frontend/` prints:

```
npm warn deprecated next@15.1.3: This version has a security vulnerability.
Please upgrade to a patched version.
See https://nextjs.org/blog/CVE-2025-66478 for more details.
```

`frontend/package.json` pins `"next": "15.1.3"`.

This was not something we went looking for. The package manager said it out loud
the first time anyone installed dependencies in this directory — which suggests
nobody has, or nobody read the output.

## Why we are not fixing it in this pull request

Upgrading the web framework is not a change to make inside a pull request about
adding a summary panel. A Next major- or minor-version bump can move the App
Router, the build output, and the route-handler contract, and this repo has *no
frontend test coverage* older than this PR to catch a regression. Bundling it
here would mean the reviewer cannot tell which change broke what.

It also should not be done blind. We do not yet know:

- **Which CVE this is and whether Riverbend is exposed.** Some Next advisories only affect specific deployment shapes — middleware, image optimisation, self-hosted vs managed. The portal may or may not use the vulnerable path.
- **What the patched version is**, and whether it is a patch bump or a minor.
- **Whether anything else in the tree is affected.** `npm audit` has never been run here, and CI does not run it.

Answering those is a small, bounded piece of work. Guessing at them inside an
unrelated PR is not.

## Why it matters here specifically

This is a **patient portal**. Whatever the vulnerability class, the thing behind
the framework is a login form and a chart viewer. The generic severity of a Next
advisory is not the severity here.

It also compounds two findings already on the register:

- **D9** — credentials were in the repository, and are still un-rotated. A framework vulnerability plus a live credential is a materially different situation from either alone.
- **D2/D14** — there is no access trail that could answer whether an exploitation attempt happened.

Under **164.308(a)(1)(ii)(A)** a risk analysis is meant to be an accurate
assessment of vulnerabilities to ePHI. A known-vulnerable framework that nobody
has assessed is exactly the gap that section describes.

## What we recommend

1. **Read the advisory and determine exposure.** Whether the portal uses the affected path is a ten-minute question with a definite answer.
2. **Upgrade to the patched version in its own PR**, with the component tests added here as the regression net. That net did not exist before today, which is part of why the upgrade is safer now than it was last week.
3. **Add `npm audit --audit-level=high` to CI.** The warning above was printed to a terminal nobody was reading. A CI step is read by definition.
4. **Run the same check against the Python services.** `pip-audit` has never been run here either, and CI does not do it.

Items 3 and 4 are the ones that stop this recurring. The specific CVE will be
fixed and forgotten; the absence of any dependency scanning is the durable
finding, and it is the same shape as the missing secret scanning recorded in
`docs/findings/w1-secrets-in-repo.md`.

## What this PR did do

Nothing to the framework. The test infrastructure added here is all
`devDependencies`, and `frontend/package.json`'s runtime dependencies are
unchanged at `next`, `react`, `react-dom` — deliberately, per `RVB-U-07`.
