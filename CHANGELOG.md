# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Centralised, testable account model (`accounts.py`)
- Shared money helpers (`money.py`) for parsing, half-up rounding, formatting and migration
- Integer-cent storage for all monetary columns, with `decimal.Decimal` used for fractional calculations and display
- One-time, idempotent migration of existing `REAL` currency values to integer cents (`PRAGMA user_version = 1`)
- Password change flow, password reset, and active/administrator user management in the admin panel
- Failed-login lockout (5 attempts / 15 minutes) and disabled-account support
- Case-insensitive username/email uniqueness and stronger password policy (8+ characters)
- Click-to-sort with visual indicators (▲/▼) on all tables
- Color-coded payment status (green for paid, red for unpaid)
- Real-time updates for client list when documents/payments change
- Enhanced .gitignore for cleaner repository
- Comprehensive documentation (README, SETUP, CONTRIBUTING)

### Changed
- Client list now shows all clients (including fully paid ones)
- Document totals, line totals, discounts, payments, client balances and exports now calculate in integer cents and round half-up to the nearest cent
- Legacy `products.price` and `sales.total_price` values are migrated to integer cents as well
- Improved sorting logic to handle "Payé" text
- Enhanced zebra striping with payment status colors
- Backups now use SQLite's backup API for a consistent snapshot (including WAL data)
- Admin restore now validates the candidate database, creates an automatic safety backup, swaps the file atomically, and returns to the login screen

### Fixed
- HTML and PDF exports now escape customer and document content, closing an HTML-injection path and preventing ReportLab markup breakage
- Export labels, status, totals and print/page text now follow the active EN/FR language, and the exported HTML declares the matching `lang` attribute
- Removed the hardcoded `admin/admin123` bootstrap; first-run admin now gets a one-time random password
- Session is no longer trusted for admin rights: role and active status are re-checked before admin actions
- Last active administrator, self-deletion, and self-deactivation are now prevented
- Restoring an invalid or incompatible database can no longer corrupt or replace the live database
- A failed post-restore migration automatically puts the previous database back
- Client list not updating after document creation
- Client list not updating after document conversion
- Client list not updating after document validation
- Sorting issues with "Payé" text in payment column

## [0.1.7] - 2025-12-28

### Added
- Initial release with core features
- Articles management
- Clients management
- Documents management (Devis, BL, Factures, Réception)
- Payments tracking
- Export to HTML/PDF
- Print support
- Discount management
- TVA calculations

---

## Version History

### 0.1.7 beta
- Core functionality implemented
- Basic UI/UX
- SQLite database
- Export features

### 0.1.0 beta
- Initial beta release
- Basic document management

---

## Release Process

1. Update version in code
2. Update CHANGELOG.md
3. Create git tag
4. Push to repository
5. Create GitHub release

## Breaking Changes

None yet.

## Known Issues

- Single-user only (no multi-user support)
- No cloud sync
- Desktop only (no mobile/web version)

## Future Roadmap

- [ ] Multi-user authentication
- [ ] Cloud synchronization
- [ ] Mobile application
- [ ] Advanced analytics
- [ ] API for integrations
- [ ] Email notifications
- [ ] SMS integration
- [ ] Barcode scanning