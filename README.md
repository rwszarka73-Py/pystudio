# PyStudio

**A Visual Studio-style Python IDE for Ubuntu**, built with PySide6. It gives you a form designer, code editor, debugger, databases, Git and publishing in one window, so you can build Python desktop apps the way you'd build WinForms apps in Visual Studio.

Current version: **2.10.14**

---

## Features

### Design
- Start Window, project templates, and solutions with several projects and project references
- **Form designer** with a Toolbox, a full Properties window, an events list, snaplines, and an in-place menu designer
- Multiple forms and dialogs, UserControls, components (Timer, NotifyIcon, ErrorProvider, BindingSource, ContextMenuStrip), charts, a web browser control, and printing
- **Qt Designer (.ui) form editor** that keeps layouts (main windows, dialogs, and panels)
- **Hot Reload**: change a running form's controls without restarting
- **Report designer**

### Code
- Code editor with **IntelliSense**, Quick Fix (Ctrl+.), Rename, Extract Method/Variable, multiple carets, and code formatting
- Go To All, Go To Definition (F12), Find All References, Back/Forward, bookmarks, reference counts, and Class View
- Error List, Task List, and Find/Replace in Files
- **F1** opens the help page for the word under the cursor

### Debug and test
- **Debugger** with breakpoints, tracepoints, hit counts, and Step Over/Into/Out
- Run To Cursor and Set Next Statement
- **Edit and Continue**
- Locals, Watch, Call Stack, Threads, the **Immediate window**, and Exception Settings
- **Attach to Process**
- **Test Explorer** (pytest) with **code coverage**
- **Performance Profiler**
- Jupyter **notebooks**

### Data
- **Server Explorer**: SQLite, IBM DB2, ODBC (DB2 for i/AS400, SQL Server, MySQL and others), and **Snowflake** (including key-pair sign-in)
- **Query Designer** with a diagram view and Excel export
- Data-bound controls you can edit and save, master/detail views, and a BindingNavigator
- **Import Data** from CSV or Excel into a table on any connection

### Source control
- **Git**: Git Changes, history, blame, branches, and a merge editor
- **Publish to GitHub** in one click: it creates the repository from the project's name and pushes
- GitHub **pull requests** and **issues** (with *Fixes #N* in commit messages)
- **Create GitHub Release**: puts your published `.deb`, `.tar.gz`, `.exe` and Setup.exe on the project's Releases page
- **New Project** can publish the project to GitHub right away

### AI assistant
- Chat about your code, plus Explain, Fix Errors, Translate VB/C#, and Write Tests
- **Inline suggestions** (Tab accepts)
- Choose your AI:
  - **Claude**: paid per use, with an API key from console.anthropic.com
  - **Gemini**: free tier, with a key from aistudio.google.com/apikey
  - **Local (Ollama)**: free and private, nothing leaves your computer; installed with one click
  - **Other**: ChatGPT, OpenRouter, Groq, Mistral, DeepSeek, xAI, LM Studio, or any other AI that accepts OpenAI's request format

### Packages and publishing
- **Package manager** (pip, like NuGet): Browse, Installed, and **Updates**
- PyStudio checks for package updates when a project opens and shows the count in the status bar
- **Publish** for Linux as a folder or single file, with install.sh and an optional `.deb`
- **Publish for Windows**: a `.exe` and a **Setup.exe** installer, built locally with Wine or in the cloud with GitHub Actions

### Moving from VB.NET
- **Import VB.NET and C# projects**: Form1.Designer.vb or .cs becomes a PyStudio form, with controls, properties, menus, and events wired up
- PyStudio helpers that feel like VB: `msgbox`, `my.settings`, and `my.query`

### Other
- Built-in **Terminal**
- Window layouts and themes
- App settings and resources
- **Import/Export Settings**, to move your setup to another machine (passwords and keys are never exported)
- **Check for Updates** from inside PyStudio
- PyStudio's own error log: Help > View Log

---

## Requirements

- Ubuntu (or another Debian-based Linux) with its own Python 3; install.sh adds `python3-venv` if it's missing
- An internet connection for the first install

## Install

1. Download `PyStudio-vX.Y.Z.zip` from the [Releases](../../releases) page and extract it.
2. Open a terminal in the extracted **PyStudio** folder.
3. Run:

```bash
bash install.sh
```

The installer sets up PyStudio's own Python environment, a menu entry, and a desktop icon. It then asks about each optional program, one question at a time (Enter means yes). Programs that are already installed are skipped.

| Option | What it does |
|---|---|
| `bash install.sh --all` | Installs every optional program without asking |
| `bash install.sh --minimal` | Skips the optional programs |

You can add the optional programs later from **Tools > Install Components**.

### Optional programs

| Program | Used for |
|---|---|
| Git | Source control |
| GitHub CLI (`gh`) | Publish to GitHub, pull requests, Run on GitHub |
| Wine | Building the Windows `.exe` on this computer |
| NSIS | The Windows Setup.exe installer |
| unixODBC | ODBC data connections |
| gdb | Debug > Attach to Process |

## Update

Use **Help > Check for Updates** inside PyStudio, or download the new zip and run `bash install.sh` again. Your settings and projects are kept.

## Uninstall

```bash
bash uninstall.sh
```

This removes PyStudio and leaves your projects alone.

---

## Getting started

1. **File > New Project** (Ctrl+Shift+N), then pick a template.
2. Drag controls from the **Toolbox** onto the form and set them in **Properties**.
3. Double-click a control to write its event code.
4. Press **F5** to run, or **F9** to set a breakpoint and debug.
5. In **Git Changes**, click **Publish to GitHub** to back the project up.

The complete guide is **README.pdf** in the download, and also **Help > PyStudio Guide** inside the app.

## Keyboard shortcuts (the Visual Studio ones)

| Action | Keys | Action | Keys |
|---|---|---|---|
| Run / Continue | F5 | Stop | Shift+F5 |
| Step Over / Into / Out | F10 / F11 / Shift+F11 | Toggle breakpoint | F9 |
| View Code / Designer | F7 / Shift+F7 | Quick Fix | Ctrl+. |
| Go To Definition | F12 | Find All References | Shift+F12 |
| Rename | F2 | Help on word | F1 |
| AI Assistant | Ctrl+Alt+A | Terminal | Ctrl+` |

---

## Privacy

- API keys and database passwords are kept in the Ubuntu keyring. They are never stored in projects or in exported settings.
- With **Gemini's free tier**, Google may use what you send to improve its products, so keep work code and data out of it. Use the local model for those.
- The **local (Ollama)** model runs entirely on your computer.

## License

Personal project by Rob Szarka. All rights reserved.
