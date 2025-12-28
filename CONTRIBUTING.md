# Contributing to Gestion Commercial Simple

Thank you for your interest in contributing to Gestion Commercial Simple!

## How to Contribute

### 1. Reporting Issues
If you find a bug or have a feature request:
1. Check if the issue already exists in the GitHub Issues
2. Create a new issue with a clear description
3. Include steps to reproduce (for bugs)
4. Add screenshots if applicable

### 2. Development Setup

#### Prerequisites
- Python 3.11+
- Git

#### Setup Steps
```bash
# Clone the repository
git clone https://github.com/yourusername/gestion-commercial-simple.git
cd gestion-commercial-simple

# Install dependencies
pip install -r requirements.txt

# Run the application
python main.py
```

### 3. Making Changes

#### Branch Naming
- Use descriptive branch names
- Prefix with type: `feature/`, `bugfix/`, `docs/`, `refactor/`
- Example: `feature/add-export-to-excel`

#### Commit Messages
Use clear, descriptive commit messages:
```
Add: New export feature for Excel
Fix: Client list not updating after payment
Update: Improve sorting performance
```

#### Code Style
- Follow PEP 8 guidelines
- Use descriptive variable names
- Add comments for complex logic
- Keep functions focused and small

### 4. Testing

Before submitting a pull request:
1. Test all affected functionality
2. Verify no regressions
3. Check edge cases
4. Test on different platforms if possible

### 5. Pull Request Process

1. Fork the repository
2. Create your feature branch
3. Commit your changes
4. Push to your fork
5. Create a Pull Request

#### PR Description Template
```markdown
## Description
Brief description of changes

## Type of Change
- [ ] Bug fix
- [ ] New feature
- [ ] Breaking change
- [ ] Documentation update

## Testing
Describe how you tested the changes

## Screenshots
If applicable, add screenshots

## Checklist
- [ ] Code follows project style
- [ ] Tests pass
- [ ] Documentation updated
- [ ] No merge conflicts
```

### 6. Code Review

All submissions require review:
- Code quality and style
- Functionality verification
- Security considerations
- Performance impact

### 7. Areas for Contribution

#### High Priority
- Bug fixes
- Performance improvements
- UI/UX enhancements
- Documentation

#### Future Features
- Multi-user support
- Cloud sync
- Mobile app
- Advanced reporting
- API integration

### 8. Development Guidelines

#### Architecture
- Keep UI and business logic separate
- Use descriptive function names
- Avoid global variables
- Handle errors gracefully

#### Database
- Use SQLite for simplicity
- Maintain backward compatibility
- Add migration scripts for schema changes

#### UI/UX
- Keep interface intuitive
- Follow platform conventions
- Support accessibility
- Test on different screen sizes

### 9. Getting Help

- Check the README.md
- Review existing code patterns
- Ask in GitHub Discussions
- Join our community (if available)

### 10. License

By contributing, you agree that your contributions will be licensed under the MIT License.

---

**Thank you for contributing!** 🎉