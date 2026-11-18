# 0005: Prism is licensed under AGPL-3.0-only

Status: accepted

## Context

The PRD left the license open, with Apache-2.0 or MIT as candidates. The
maintainer does not want third parties to build commercial products on Prism
without contributing back or paying for a license.

A license that bans commercial use outright, such as PolyForm Noncommercial,
would make Prism source-available rather than open source. It would also stop
companies from running Prism internally, which works against the PRD's goals and
target users.

## Decision

Prism is licensed under the GNU Affero General Public License, version 3 only
(`AGPL-3.0-only`). A separate commercial license is offered to organisations
that cannot meet the AGPL's terms (dual licensing).

The "only" variant is used so that a future AGPL version cannot change the
terms without the maintainer's choice.

## Consequences

- Prism remains open source by the OSI definition.
- Anyone may use and self-host Prism, including commercially.
- Anyone who modifies Prism and lets others use it over a network must publish
  their modified source under the same license (section 13). This deters
  closed, hosted forks.
- Some companies have policies against using AGPL software. The commercial
  license gives them a way to use Prism.
- Outside contributions need a Contributor License Agreement, so the project
  can continue to offer the commercial license. No external pull requests are
  accepted until the CLA exists.
- Prism's dependencies use permissive licenses (MIT, BSD, Apache-2.0), which are
  compatible with the AGPL. New dependencies must be checked for compatibility.
- The PRD's summary table ("Apache-2.0 (proposed)") and its license open
  question are superseded by this record.
