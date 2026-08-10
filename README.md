# Gestion Commercial Simple (GCS)

A comprehensive commercial management application built with Python and customtkinter for small to medium businesses.

## Features

### Core Modules
- **Articles Management**: Add, edit, delete products with stock tracking
- **Clients Management**: Maintain customer database with contact information
- **Documents Management**: Create and manage quotes (Devis), delivery notes (BL), invoices (Factures), and receipts (Réception)
- **Payments Tracking**: Monitor client payments and outstanding amounts
- **Real-time Updates**: Automatic refresh of client balances when documents or payments are modified
- **Secure Authentication**: Passwords stored with PBKDF2-HMAC-SHA256 hashing (random salt, 600,000 iterations)

### Document Features
- Click-to-sort tables with visual indicators (▲/▼)
- Zebra striping for better readability
- Export to HTML and PDF
- Print support
- Discount management
- TVA (VAT) calculations
- Payment integration

### User Interface
- Modern dark/light mode support
- Intuitive navigation
- Real-time search and autocomplete
- Responsive dialogs
- Color-coded payment status (green for paid, red for unpaid)

## Installation

### Prerequisites
- Python 3.11 or higher
- pip package manager

### Setup

1. Clone the repository:
```bash
git clone https://github.com/yourusername/gestion-commercial-simple.git
cd gestion-commercial-simple
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Run the application:
```bash
python main.py
```

## Usage

### First Launch
1. The application will create a database automatically in the application directory
2. Login with the default admin account (`admin` / `admin123`) or create your first user
3. Start adding clients, articles, and documents

### Navigation
- **Articles**: Manage your product catalog
- **Clients**: Maintain customer database
- **Documents**: Create quotes, delivery notes, invoices
- **Paiements**: Track payments and outstanding amounts
- **Admin**: Database backup/restore settings

### Keyboard Shortcuts
- Double-click on items to edit
- Click table headers to sort
- Press Enter to search
- Press Escape to close dialogs

> **Security note**: The default admin credentials are `admin` / `admin123` — use them only on first launch and change the password before real deployment.

## Project Structure

```
GCS/
├── main.py              # Login and user authentication
├── main_app.py          # Main application (7900+ lines)
├── requirements.txt     # Python dependencies
├── README.md           # This file
├── .gitignore          # Git ignore rules
├── assets/
│   └── logo.png        # Application logo
├── tests/              # Automated tests (pytest)
└── commercial.db       # SQLite database (created on first run)
```

## Dependencies

- **customtkinter** >= 5.2.0 - Modern UI components
- **pillow** >= 10.0.0 - Image processing
- **reportlab** >= 4.0.0 - PDF generation
- **pytest** (development only) - Test framework

## Security

- Passwords are hashed with **PBKDF2-HMAC-SHA256** (random 16-byte salt, 600,000 iterations) before being stored
- Existing plaintext passwords are automatically migrated to hashes on the next successful login
- The database file (`commercial.db`) is always stored in the application directory, regardless of the working directory it is launched from
- Destructive administrative actions (e.g. database wipe, restore) require re-entering your password and offer a backup of the current database

## Development

### Code Structure
The application follows a modular structure:
- **UI Layer**: customtkinter widgets and dialogs
- **Business Logic**: Document calculations, payment processing
- **Data Layer**: SQLite database operations
- **Export Layer**: HTML and PDF generation

### Testing
Run the test suite with:

```bash
python -m pytest tests
```

Tests cover password hashing, database schema initialization, document numbering sequences, and database path resolution. They run headless (no GUI required).

### Key Features Implementation
- **Sorting**: Custom sorting functions with visual feedback
- **Zebra Striping**: Automatic alternating row colors
- **Real-time Updates**: Event-driven refresh of dependent data
- **Color Coding**: Tag-based styling for payment status

## Contributing

Contributions are welcome! Please follow these steps:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/YourFeature`)
3. Commit your changes (`git commit -m 'Add YourFeature'`)
4. Push to the branch (`git push origin feature/YourFeature`)
5. Open a Pull Request

### Code Style
- Use descriptive variable names
- Add comments for complex logic
- Follow PEP 8 guidelines
- Test your changes before committing

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Support

For support and questions:
- Check the documentation in the code
- Review the README files
- Open an issue on GitHub

## Acknowledgments

- Built with customtkinter for a modern UI
- Uses SQLite for reliable data storage
- PDF generation with reportlab
- Image handling with Pillow

---

**Note**: This is a desktop application designed for single-user or small team usage. For multi-user scenarios, consider implementing user authentication and network support.
