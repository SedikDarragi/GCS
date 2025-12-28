# Setup Guide

## Quick Start

### 1. Install Python
Make sure you have Python 3.11 or higher installed:
```bash
python --version
```

### 2. Clone or Download
```bash
git clone https://github.com/yourusername/gestion-commercial-simple.git
cd gestion-commercial-simple
```

### 3. Install Dependencies
```bash
pip install -r requirements.txt
```

### 4. Run the Application
```bash
python main.py
```

## First Time Setup

When you run the application for the first time:
1. The database will be created automatically
2. You'll see a login window
3. Click "Créer un compte" to create your first user
4. Start using the application

## Troubleshooting

### Issue: "Module not found" errors
**Solution**: Make sure all dependencies are installed:
```bash
pip install customtkinter pillow reportlab
```

### Issue: Database errors
**Solution**: Delete the `commercial.db` file and restart the application. It will be recreated.

### Issue: UI not displaying correctly
**Solution**: Try changing the appearance mode:
```python
# In main.py or main_app.py
ctk.set_appearance_mode("dark")  # or "light"
```

## Development Setup

### Recommended IDE
- Visual Studio Code with Python extension
- PyCharm Community/Professional

### Code Structure
```
GCS/
├── main.py              # Entry point & login
├── main_app.py          # Main application
├── requirements.txt     # Dependencies
├── assets/              # Images & resources
│   └── logo.png
└── commercial.db        # SQLite database (auto-created)
```

### Running Tests
Currently, the application doesn't have automated tests. Manual testing is recommended:
1. Create a client
2. Add articles
3. Create documents
4. Add payments
5. Verify real-time updates

## Deployment

### For Windows
1. Install PyInstaller:
```bash
pip install pyinstaller
```

2. Create executable:
```bash
pyinstaller --onefile --windowed --icon=assets/logo.png main.py
```

### For Linux/Mac
Similar process, but may require additional configuration for the GUI.

## Support

For issues or questions:
1. Check the README.md
2. Review the code comments
3. Open a GitHub issue

## License

This project is licensed under the MIT License.