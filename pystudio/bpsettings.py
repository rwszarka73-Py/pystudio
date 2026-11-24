"""Breakpoint Settings (VS): stop only when a condition is true or on a hit count, and/or log a message to Output
- with "Continue code execution" checked it's a tracepoint: it logs and never stops."""
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QSpinBox, QVBoxLayout)

HIT_MODES = [("", "(any hit)"), ("=", "is equal to"), (">=", "is greater than or equal to"), ("%", "is a multiple of")]


class BreakpointSettingsDialog(QDialog):
    def __init__(self, cond="", hit=("", 0), log="", cont=False, line=0, focus="conditions", parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Breakpoint Settings - line {line}" if line else "Breakpoint Settings")
        self.resize(560, 360)
        v = QVBoxLayout(self)

        self.gb_cond = QGroupBox("Conditions")
        self.gb_cond.setCheckable(True)
        self.gb_cond.setChecked(bool(cond or hit[0]))
        cl = QVBoxLayout(self.gb_cond)
        row = QHBoxLayout()
        row.addWidget(QLabel("Stop when this is true:"))
        self.cond = QLineEdit(cond)
        self.cond.setPlaceholderText("e.g.  total > 100   or   name == 'Rob'")
        row.addWidget(self.cond, 1)
        cl.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(QLabel("and the hit count"))
        self.hit_mode = QComboBox()
        for k, label in HIT_MODES:
            self.hit_mode.addItem(label, k)
        self.hit_mode.setCurrentIndex(max(0, [k for k, _l in HIT_MODES].index(hit[0]) if hit[0] in
                                             [k for k, _l in HIT_MODES] else 0))
        self.hit_n = QSpinBox()
        self.hit_n.setRange(1, 10_000_000)
        self.hit_n.setValue(max(1, int(hit[1] or 1)))
        self.hit_mode.currentIndexChanged.connect(lambda _i: self.hit_n.setEnabled(bool(self.hit_mode.currentData())))
        self.hit_n.setEnabled(bool(hit[0]))
        row.addWidget(self.hit_mode)
        row.addWidget(self.hit_n)
        row.addStretch()
        cl.addLayout(row)
        tip = QLabel("The hit count counts the times the line is reached with the condition true; it starts over "
                     "when you change these settings or start debugging again.")
        tip.setWordWrap(True)
        tip.setStyleSheet("color: #9a9a9a;")
        cl.addWidget(tip)
        v.addWidget(self.gb_cond)

        self.gb_act = QGroupBox("Actions")
        self.gb_act.setCheckable(True)
        self.gb_act.setChecked(bool(log))
        al = QVBoxLayout(self.gb_act)
        row = QHBoxLayout()
        row.addWidget(QLabel("Show a message in Output:"))
        self.log = QLineEdit(log)
        self.log.setPlaceholderText("e.g.  Order {order.id} total={total:.2f}  ($FUNCTION, line $LINE)")
        row.addWidget(self.log, 1)
        al.addLayout(row)
        help_ = QLabel("{expression} shows a value. Also: $FUNCTION, $CALLER, $LINE, $FILE, $TNAME (thread), $TID, "
                       "$HITCOUNT.")
        help_.setWordWrap(True)
        help_.setStyleSheet("color: #9a9a9a;")
        al.addWidget(help_)
        self.cont = QCheckBox("Continue code execution (a tracepoint: log the message and don't stop)")
        self.cont.setChecked(cont)
        al.addWidget(self.cont)
        v.addWidget(self.gb_act)
        v.addStretch()
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        if focus == "actions":
            self.gb_act.setChecked(True)
            self.log.setFocus()
            self.log.selectAll()
        else:
            self.gb_cond.setChecked(True)
            self.cond.setFocus()

    def values(self):
        """(condition, (hit mode, n), log message, continue)."""
        cond, hit = "", ("", 0)
        if self.gb_cond.isChecked():
            cond = self.cond.text().strip()
            if self.hit_mode.currentData():
                hit = (self.hit_mode.currentData(), self.hit_n.value())
        log, cont = "", False
        if self.gb_act.isChecked() and self.log.text().strip():
            log, cont = self.log.text().strip(), self.cont.isChecked()
        return cond, hit, log, cont
