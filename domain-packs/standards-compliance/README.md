# Standards Compliance Domain Pack

Reusable guidance for research workspaces that answer standards, conformity,
product-requirement, or standards-registry questions.

Use this pack when answers depend on exact standard designation, edition or
year, current status, registry authority, legal/product linkage, or register
inclusion. It is guidance-only: it does not enable acquisition, crawling, or
provider access.

## Coverage Templates

- `coverage-templates/official-standard-reference.yml`: exact standards-body
  catalogue or registry identity.
- `coverage-templates/standards-current-version.yml`: current status,
  replacement-chain, and designation checks for a cited standard.
- `coverage-templates/eu-product-requirement-profile.yml`: EU product
  requirement, harmonised-standard, OJEU, and legal-act linkage.
- `coverage-templates/uk-geospatial-standard-register-entry.yml`: GOV.UK
  geospatial register inclusion and linked owner-reference handling.

## Acquisition Boundaries

Discovery and acquisition remain explicit workflow steps. Use selected
standards candidates and bounded `web get` captures with standards sidecars.
ISO Open Data metadata is useful for registry identity, but this pack does not
recommend or enable a provider-native `iso-open-data` acquisition adapter.

Do not store full ISO, IEC, BSI, CEN/CENELEC, ETSI, or other restricted
standards text unless a reviewed license or open-data route permits it.
