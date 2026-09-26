"""
📖 What Can I Say? Manual Dialog

A reference guide for users to understand how Toustovač's behavior changes
based on its current Presence Mode.
"""

from typing import Dict, Any
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QScrollArea, QWidget, QFrame
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont

from desktop_app.themes import COLORS, JARVIS_THEME_STYLESHEET
from jarvis.presence import PresenceMode


class ModeCard(QFrame):
    """A visual card representing a single Presence Mode."""
    def __init__(self, mode: PresenceMode, title: str, description: str, examples: list[str]):
        super().__init__()
        self.setObjectName("mode_card")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setLineWidth(0)
        self.setStyleSheet(f"""
            QWidget#mode_card {{
                background-color: {COLORS['bg_secondary']};
                border: 1px solid {COLORS['border']};
                border-radius: 16px;
                padding: 16px;
            }}
        """)
        
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        # Title & Emoji
        header_layout = QHBoxLayout()
        title_label = QLabel(title)
        title_label.setStyleSheet(f"color: {COLORS['accent_primary']}; font-size: 18px; font-weight: bold;")
        header_layout.addWidget(title_label)
        layout.addLayout(header_layout)

        # Description
        desc_label = QLabel(description)
        desc_label.setWordWrap(True)
        desc_label.setStyleSheet(f"color: {COLORS['text_primary']}; font-size: 14px;")
        layout.addWidget(desc_label)

        # Examples section
        if examples:
            example_title = QLabel("💬 Examples:")
            example_title.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 12px; font-weight: bold; margin-top: 8px;")
            layout.addWidget(example_title)

            for ex in examples:
                ex_label = QLabel(f"• {ex}")
                ex_label.setWordWrap(True)
                ex_label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 13px; padding-left: 4px;")
                layout.addWidget(ex_label)

        layout.addStretch()


class WhatCanISayDialog(QDialog):
    """Dialog presenting the 'What can I say?' manual."""

    MODE_CONTENT: Dict[PresenceMode, Dict[str, Any]] = {
        PresenceMode.PASSIVE: {
            "title": "Passive Mode",
            "emoji": "☁️",
            "desc": "Toustovač is in the background. It won't interrupt you or react to everything. It's just 'present'.",
            "examples": ["(Just working normally)", "No commands needed."]
        },
        PresenceMode.ADDRESSED: {
            "title": "Addressed Mode",
            "emoji": "👂",
            "desc": "You just spoke the wake word. Toustovač is listening intently to your next command.",
            "examples": ["'Hey Jarvis, what's the weather?'", "'Jarvis, play some jazz.'"]
        },
        PresenceMode.CONVERSATION: {
            "title": "Conversation Mode",
            "emoji": "💬",
            "desc": "Active multi-turn dialogue. Toustovač is fully engaged in a back-and-forth with you.",
            "examples": ["'Tell me more about that.'", "'Actually, I meant something else.'"]
        },
        PresenceMode.COMPANION: {
            "title": "Companion Mode",
            "emoji": "🤖",
            "desc": "Proactive/reactive engagement. Toustovač might chime in with relevant observations or humor.",
            "examples": ["(Natural interaction)", "'That's a nice view you've got there.'"]
        },
        PresenceMode.QUIET_COMPANY: {
            "title": "Quiet Company",
            "emoji": "🧘",
            "desc": "Presence acknowledged, but staying out of the way. Perfect for working without distraction.",
            "examples": ["(Working quietly)", "'I'm here if you need me.'"]
        },
        PresenceMode.FOCUS: {
            "title": "Focus Mode",
            "emoji": "🎯",
            "desc": "You are busy. Toustovač will minimize interruptions and avoid spontaneous remarks.",
            "examples": ["(Focusing on work)", "'Don't interrupt me right now.'"]
        },
        PresenceMode.QUIET: {
            "title": "Quiet Mode",
            "emoji": "🤫",
            "desc": "Explicit 'Do Not Disturb'. Toustovač is silent and won't respond unless woken.",
            "examples": ["'Go to sleep.'", "'Be quiet for a while.'"]
        },
    }

    # Terminal Command Composer card (K8) — appended after the mode cards.
    TERMINAL_CARD = {
        "title": "Terminal",
        "emoji": "⌨️",
        "desc": "In a focused terminal Toustovač prepares exactly one "
                "command line for the detected shell and inserts it without "
                "pressing Enter. You stay the execution authority.",
        "examples": [
            "Toustovač, připrav příkaz na posledních sto Docker logů.",
            "Toustovač, najdi proces na portu 8080.",
            "Toustovač, teď ten kontejner restartuj.",
            "Toustovač, připrav totéž pro Bash.",
        ],
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("📖 What can I say?")
        self.setMinimumSize(500, 600)
        self.setStyleSheet(JARVIS_THEME_STYLESHEET)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(24, 24, 24, 24)
        main_layout.setSpacing(16)

        # Header
        header = QLabel("How to interact with Toustovač")
        header.setObjectName("title")
        header.setStyleSheet("font-size: 22px; font-weight: bold; color: #fbbf24;")
        main_layout.addWidget(header)

        intro = QLabel("Toustovač's behavior changes depending on its current mode. Use the cards below to see what to expect.")
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #a1a1aa; font-size: 14px;")
        main_layout.addWidget(intro)

        # Scroll Area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(f"background-color: transparent; border: none;")
        
        container = QWidget()
        container.setStyleSheet("background-color: transparent;")
        container_layout = QVBoxLayout(container)
        container_layout.setSpacing(16)
        container_layout.setContentsMargins(0, 0, 0, 0)

        for mode, content in self.MODE_CONTENT.items():
            card = ModeCard(
                mode,
                f"{content['emoji']} {content['title']}",
                content['desc'],
                content['examples']
            )
            container_layout.addWidget(card)

        t = WhatCanISayDialog.TERMINAL_CARD
        container_layout.addWidget(ModeCard(
            PresenceMode.CONVERSATION,
            f"{t['emoji']} {t['title']}",
            t['desc'],
            t['examples'],
        ))

        container_layout.addStretch()
        scroll.setWidget(container)
        main_layout.addWidget(scroll)

        # Close Button
        close_btn = QPushButton("Got it!")
        close_btn.setFixedWidth(120)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {COLORS['accent_primary']};
                color: #0a0b0f;
                border-radius: 12px;
                padding: 8px 16px;
                font-weight: 600;
            }}
            QPushButton:hover {{
                background-color: {COLORS['accent_secondary']};
            }}
        """)
        close_btn.clicked.connect(self.accept)
        main_layout.addWidget(close_btn, alignment=Qt.AlignmentFlag.AlignCenter)
