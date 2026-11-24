"""Code snippets: type the shortcut, press Tab (VS: 'tab tab'), fill in the highlighted fields.

${1:text} marks a field (Tab moves to the next one); $0 is where the cursor ends up.
"""
import re

SNIPPETS = {
    "def": ("Function", "def ${1:name}(${2:}):\n    ${0:pass}"),
    "defs": ("Method (with self)", "def ${1:name}(self${2:}):\n    ${0:pass}"),
    "if": ("if", "if ${1:condition}:\n    ${0:pass}"),
    "ife": ("if / else", "if ${1:condition}:\n    ${2:pass}\nelse:\n    ${0:pass}"),
    "elif": ("elif", "elif ${1:condition}:\n    ${0:pass}"),
    "for": ("for loop", "for ${1:item} in ${2:items}:\n    ${0:pass}"),
    "fori": ("for i in range", "for ${1:i} in range(${2:10}):\n    ${0:pass}"),
    "while": ("while loop", "while ${1:condition}:\n    ${0:pass}"),
    "try": ("try / except", "try:\n    ${1:pass}\nexcept ${2:Exception} as ${3:e}:\n    ${0:print(e)}"),
    "tryf": ("try / finally", "try:\n    ${1:pass}\nfinally:\n    ${0:pass}"),
    "with": ("with (open a file)", "with open(${1:\"file.txt\"}, ${2:\"r\"}) as ${3:f}:\n    ${0:text = f.read()}"),
    "class": ("Class", "class ${1:Name}:\n    def __init__(self${2:}):\n        ${0:pass}"),
    "main": ("if __name__ == '__main__'", "if __name__ == \"__main__\":\n    ${0:main()}"),
    "msgbox": ("Message box (like VB MsgBox)",
               "pystudio_runtime.msgbox(${1:\"Hello!\"}, ${2:\"Title\"}, ${3:\"OK\"}, ${4:\"Information\"})$0"),
    "ask": ("Yes/No question",
            "if pystudio_runtime.msgbox(${1:\"Are you sure?\"}, ${2:\"Confirm\"}, \"YesNo\", \"Question\") == \"Yes\":\n    ${0:pass}"),
    "dlg": ("Show a form as a dialog (VB ShowDialog)",
            "from ${1:dialog1} import ${2:Dialog1}\ndlg = ${3:Dialog1}()\nif dlg.show_dialog(self) == \"OK\":\n    ${0:pass}"),
    "showform": ("Open another form (VB Form2.Show)",
                 "from ${1:form2} import ${2:Form2}\nself.${3:form2} = ${4:Form2}()\nself.${5:form2}.show()$0"),
    "printpage": ("PrintDocument printPage drawing",
                  "y = e.margin_bounds.top() + 20\nfor line in ${1:lines}:\n    e.graphics.drawText(int(e.margin_bounds.left()), int(y), str(line))\n    y += ${2:20}$0"),
    "openfile": ("Read a text file", "with open(${1:path}, encoding=\"utf-8\") as f:\n    ${2:text} = f.read()$0"),
    "savefile": ("Write a text file", "with open(${1:path}, \"w\", encoding=\"utf-8\") as f:\n    f.write(${2:text})$0"),
    "prop": ("Property", "@property\ndef ${1:name}(self):\n    return ${0:None}"),
    "pr": ("print", "print(${1:})$0"),
    "connect": ("Connect a signal", "self.ui.${1:button1}.${2:clicked}.connect(self.${3:handler})$0"),
}

_FIELD = re.compile(r"\$\{(\d+):([^}]*)\}|\$(\d+)")


def expand(key, indent):
    """Return (text, fields) where fields = [(order, start, end)] offsets in text."""
    body = SNIPPETS[key][1].replace("\n", "\n" + indent)
    text = ""
    fields = []
    pos = 0
    for m in _FIELD.finditer(body):
        text += body[pos:m.start()]
        order = int(m.group(1) or m.group(3))
        default = m.group(2) or ""
        fields.append((order if order else 999, len(text), len(text) + len(default)))
        text += default
        pos = m.end()
    text += body[pos:]
    fields.sort(key=lambda f: f[0])
    return text, fields
