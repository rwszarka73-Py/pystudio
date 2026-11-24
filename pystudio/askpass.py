"""Git sign-in prompt (GIT_ASKPASS / SSH_ASKPASS): git runs this when it needs a user name,
password/token or SSH key passphrase. Prints the answer; exit code 1 = canceled."""
import os
import sys

from PySide6.QtWidgets import QApplication, QInputDialog, QLineEdit


def main():
    prompt = " ".join(sys.argv[1:]).strip() or "Password:"
    app = QApplication(sys.argv[:1])
    app.setApplicationName("PyStudio")
    app.setDesktopFileName("pystudio")
    secret = any(w in prompt.lower() for w in ("password", "passphrase", "token"))
    hint = ""
    if "github.com" in prompt.lower() and "password" in prompt.lower():
        hint = ("\n\nGitHub doesn't take your account password here - paste a personal access token\n"
                "(github.com > Settings > Developer settings > Personal access tokens).")
    title = os.environ.get("PYSTUDIO_ASKPASS_TITLE") or "Git sign-in"
    if "yes/no" in prompt.lower():          # an unknown host key: PyStudio checks those itself first
        sys.exit(1)
    text, ok = QInputDialog.getText(None, title, prompt + hint,
                                    QLineEdit.Password if secret else QLineEdit.Normal)
    if not ok:
        sys.exit(1)
    print(text)


if __name__ == "__main__":
    main()
