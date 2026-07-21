# Third-Party Notices

The EvidenceWiki source distribution contains project-authored synthetic test fixtures under
`tests/fixtures/`. Those fixtures are covered by this repository's MIT license.
All other project-authored source code, documentation, templates, and examples
are also released under the repository MIT license.
Provider names, standards designations, publication identifiers, and reserved or
public URLs in a fixture are factual reference metadata; they do not indicate
that a provider response, paper, standard, or website has been redistributed.

The PDF extraction fixtures are synthetic parser inputs. Their directory names
retain historical reference identifiers so regression tests remain stable, but
their prose and layout text were authored for this project and are not excerpts
from the referenced papers.

`tests/fixtures/fixture-provenance.yml` is the authoritative path-to-rights
inventory. A release check fails when a distributed fixture is not covered by
exactly one inventory entry. If a future test genuinely requires third-party
content, its entry must record the source work, version, origin and terms URLs,
retrieval date, rights holder where known, redistribution permission,
attribution, transformations, and every shipped path before the content lands.

Runtime and development dependencies retain their own copyright and license
terms. Dependency inventories and notices do not relicense those projects.

The Python distribution declares PyYAML (MIT) and pypdf (BSD-3-Clause) as
runtime dependencies. They are resolved and installed by the user's Python
package installer; their source code is not vendored into this repository.

Poppler is an optional system dependency used only by the explicit
`pdftotext` compatibility backend. The EvidenceWiki Python wheel does not
bundle or install Poppler; the repository Containerfile installs Debian's
`poppler-utils` package into the runtime image. Poppler and that
operating-system package retain their own upstream copyright and license terms.
