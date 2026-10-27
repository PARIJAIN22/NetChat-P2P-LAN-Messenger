"""
gui_layout.py - Building the window and applying the colour themes.
"""

import tkinter as tk
from tkinter import ttk

from .gui_chatview import ChatView
from .gui_widgets import (
    FONT_BODY, FONT_SMALL, FONT_TITLE, HEADER_HEIGHT, LIGHT, SIDEBAR_WIDTH, THEMES, RoundedInput,
    ScrollFrame, apply_ttk_styles, install_mousewheel, make_app_icon, px)


class LayoutMixin:
    """Building the window and applying the colour themes. (mixed into ChatApp in gui.py)"""

    def _themed(self, widget, **options):
        """Remember that `widget` takes its colours from the theme: {option: colour key}."""
        self.themed.append((widget, options))
        return widget

    def _build_widgets(self, name, args):
        root = self.root
        root.title(self.base_title)
        root.geometry("%dx%d" % (px(900), px(600)))
        root.minsize(px(700), px(450))
        root.iconphoto(True, make_app_icon())
        # grid weights: column 0 and the body row take all the extra space when resizing
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        self.dark_var = tk.BooleanVar(value=self.settings["dark_mode"])
        self.log_var = tk.BooleanVar(value=False)
        self._build_menu()
        self._build_header(name, args)
        self._build_body()
        self._build_log_panel()
        self._build_status_bar(args)
        self._bind_keys()
        install_mousewheel(root)
        self.chat.retry_callback = self._retry
        self.chat.delete_callback = self._delete_message
        self.chat.progress = self.progress
        self.chat.file_action = self._file_action
        self.apply_theme("dark" if self.dark_var.get() else "light")
        self._show_chat_placeholder()

    def _build_menu(self):
        menubar = tk.Menu(self.root)
        chat = tk.Menu(menubar, tearoff=False)
        chat.add_command(label="Send a file...", accelerator="Ctrl+O", command=self._attach)
        chat.add_command(label="Search in this chat", accelerator="Ctrl+F", command=self._toggle_search)
        chat.add_command(label="Clear chat...", command=self._clear_chat)
        chat.add_separator()
        chat.add_command(label="New group...", command=self._new_group)
        chat.add_command(label="My fingerprint...", command=self._show_my_fingerprint)
        chat.add_command(label="Connect by IP...", command=self._connect_by_ip)
        menubar.add_cascade(label="Chat", menu=chat)
        view = tk.Menu(menubar, tearoff=False)
        view.add_checkbutton(label="Dark mode", variable=self.dark_var, command=self._on_dark_toggle)
        view.add_checkbutton(label="Network log", accelerator="Ctrl+L", variable=self.log_var,
                             command=self._apply_log_visibility)
        menubar.add_cascade(label="View", menu=view)
        tools = tk.Menu(menubar, tearoff=False)
        tools.add_command(label="Network info...", command=self._open_network_info)
        tools.add_command(label="System monitor...", command=self._open_system_monitor)
        tools.add_command(label="Settings...", command=self._open_settings)
        menubar.add_cascade(label="Tools", menu=tools)
        self.root.config(menu=menubar)

    def _build_header(self, name, args):
        header = self._themed(tk.Frame(self.root, height=px(HEADER_HEIGHT)), bg="header_bg")
        header.grid(row=0, column=0, sticky="ew")
        header.grid_propagate(False)
        header.columnconfigure(0, weight=1)
        self._themed(tk.Label(header, text="NetChat", font=(FONT_TITLE[0], 16, "bold")),
                     bg="header_bg", fg="header_fg").grid(row=0, column=0, sticky="w", padx=px(18), pady=px(10))

        right = self._themed(tk.Frame(header), bg="header_bg")
        right.grid(row=0, column=1, sticky="e", padx=px(18))
        self._themed(tk.Label(right, text=name, font=(FONT_TITLE[0], 12, "bold")),
                     bg="header_bg", fg="header_fg").pack(side="left")
        self._themed(tk.Label(right, text="  %s:%d  " % (self.peer.my_ip, args.tcp_port), font=FONT_SMALL),
                     bg="header_bg", fg="header_muted").pack(side="left")
        self.dot = self._themed(tk.Canvas(right, width=px(14), height=px(14), highlightthickness=0, bd=0),
                                bg="header_bg")
        self.dot.pack(side="left")

    def _build_body(self):
        body = tk.Frame(self.root)
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)
        self._build_sidebar(body)
        self._build_main(body)

    def _build_sidebar(self, body):
        side = self._themed(tk.Frame(body, width=px(SIDEBAR_WIDTH)), bg="side_bg")
        side.grid(row=0, column=0, sticky="ns")
        side.grid_propagate(False)               # keep the fixed sidebar width
        side.columnconfigure(0, weight=1)
        side.rowconfigure(1, weight=1)

        top = self._themed(tk.Frame(side), bg="side_bg")
        top.grid(row=0, column=0, sticky="ew", padx=px(16), pady=(px(14), px(8)))
        self._themed(tk.Label(top, text="Online peers", font=(FONT_TITLE[0], 12, "bold")),
                     bg="side_bg", fg="side_fg").pack(side="left")
        self.peer_count = self._themed(tk.Label(top, text="0", font=FONT_SMALL),
                                       bg="side_bg", fg="side_muted")
        self.peer_count.pack(side="right")
        ttk.Button(top, text="+ Group", style="Side.TButton", cursor="hand2",
                   command=self._new_group).pack(side="right", padx=(0, px(8)))

        # Rows have a fixed height and no wrapping text, so the bar may appear over the list safely.
        self.peer_list = ScrollFrame(side, LIGHT["side_bg"], "Side.Vertical.TScrollbar", reserve_bar=False)
        self.peer_list.grid(row=1, column=0, sticky="nsew")

    def _build_main(self, body):
        main = self._themed(tk.Frame(body), bg="chat_bg")
        main.grid(row=0, column=1, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(2, weight=1)

        # header of the open chat: name + online/typing status + search / clear buttons
        bar = self._themed(tk.Frame(main), bg="card_bg")
        bar.grid(row=0, column=0, sticky="ew")
        bar.columnconfigure(3, weight=1)
        self.chat_title = self._themed(tk.Label(bar, text="NetChat", font=FONT_TITLE, anchor="w"),
                                       bg="card_bg", fg="text")
        self.chat_title.grid(row=0, column=0, padx=(px(18), px(8)), pady=px(10))
        self.chat_status = self._themed(tk.Label(bar, text="", font=FONT_SMALL), bg="card_bg")
        self.chat_status.grid(row=0, column=1)
        # lock icon + the other person's key fingerprint (compare it out loud to be sure who you talk to)
        self.chat_lock = self._themed(tk.Label(bar, text="", font=FONT_SMALL), bg="card_bg")
        self.chat_lock.grid(row=0, column=2, sticky="w", padx=px(14))
        tools = self._themed(tk.Frame(bar), bg="card_bg")
        tools.grid(row=0, column=4, padx=(0, px(10)))
        for text, command in (("Search", self._toggle_search), ("Clear", self._clear_chat)):
            ttk.Button(tools, text=text, style="Tool.TButton", cursor="hand2",
                       command=command).pack(side="left", padx=px(2))
        self._themed(tk.Frame(bar, height=1), bg="card_border").grid(row=1, column=0, columnspan=5, sticky="ew")

        # search bar (hidden until Search is clicked)
        self.search_bar = self._themed(tk.Frame(main), bg="card_bg")
        self.search_bar.columnconfigure(0, weight=1)
        self.search_var = tk.StringVar()
        self.search_entry = self._themed(
            tk.Entry(self.search_bar, textvariable=self.search_var, font=FONT_BODY, relief="flat", bd=0),
            bg="chat_bg", fg="text", insertbackground="text")
        self.search_entry.grid(row=0, column=0, sticky="ew", padx=px(14), pady=px(6), ipady=px(4))
        self.search_count = self._themed(tk.Label(self.search_bar, text="", font=FONT_SMALL),
                                         bg="card_bg", fg="muted")
        self.search_count.grid(row=0, column=1, padx=px(8))
        self.search_entry.bind("<KeyRelease>", lambda e: self._render_chat(keep_scroll=False))
        self.search_entry.bind("<Escape>", lambda e: self._toggle_search())
        self.search_bar.grid(row=1, column=0, sticky="ew")
        self.search_bar.grid_remove()

        self.chat = ChatView(main, LIGHT)
        self.chat.grid(row=2, column=0, sticky="nsew")

        # input bar
        bottom = self._themed(tk.Frame(main), bg="chat_bg")
        bottom.grid(row=3, column=0, sticky="ew", padx=px(14), pady=px(12))
        bottom.columnconfigure(2, weight=1)
        self.attach_button = ttk.Button(bottom, text="\U0001F4CE", style="Icon.TButton", cursor="hand2",
                                        command=self._attach)
        self.attach_button.grid(row=0, column=0, sticky="s", padx=(0, px(2)))
        self.emoji_button = ttk.Button(bottom, text="\U0001F642", style="Icon.TButton", cursor="hand2",
                                       command=self._pick_emoji)
        self.emoji_button.grid(row=0, column=1, sticky="s", padx=(0, px(6)))
        self.input = RoundedInput(bottom, LIGHT, self._send, on_typing=self._typing_notify)
        self.input.grid(row=0, column=2, sticky="ew")
        self.send_button = ttk.Button(bottom, text="Send", style="Send.TButton", command=self._send)
        self.send_button.grid(row=0, column=3, sticky="s", padx=(px(10), 0))
        self.send_button.state(["disabled"])
        self._update_send_state()

    def _bind_keys(self):
        self.root.bind("<Control-l>", lambda e: self._toggle_log())
        self.root.bind("<Control-f>", lambda e: self._toggle_search())
        self.root.bind("<Control-o>", lambda e: self._attach())
        self.root.bind("<Escape>", lambda e: self.input.clear())
        self.root.bind("<FocusIn>", lambda e: self._stop_flash())
        # Bound on the text box too, because Tk's own Ctrl+Up/Down there would otherwise run first.
        for widget in (self.root, self.input.text):
            widget.bind("<Control-Up>", lambda e: self._move_selection(-1) or "break")
            widget.bind("<Control-Down>", lambda e: self._move_selection(1) or "break")

    def apply_theme(self, name):
        """Re-colour everything. Static widgets are in self.themed; the bubbles and peer rows
        are cheap to rebuild, so those are simply drawn again with the new colours."""
        self.theme_name = name
        t = THEMES[name]
        apply_ttk_styles(self.root, t)
        self.root.configure(bg=t["chat_bg"])
        for widget, options in self.themed:
            widget.configure(**{option: t[key] for option, key in options.items()})
        self.input.apply_theme(t)
        self.chat.set_theme(t)
        self.peer_list.set_bg(t["side_bg"])
        self.log_box.configure(bg=t["log_bg"], fg=t["log_fg"], insertbackground=t["log_fg"])
        self.log_box.tag_configure("normal", foreground=t["log_fg"])
        self.log_box.tag_configure("msg", foreground=t["accent"])
        self.log_box.tag_configure("warn", foreground=t["err"])
        self._paint_dot()
        self._refresh_list()
        self._render_chat()

    def _on_dark_toggle(self):
        self.apply_theme("dark" if self.dark_var.get() else "light")
        self._save_settings(dark_mode=self.dark_var.get())

    @property
    def t(self):
        return THEMES[self.theme_name]

    def _paint_dot(self):
        self.dot.delete("all")
        color = self.t["err"] if self.dot_error else self.t["ok"]
        self.dot.create_oval(2, 2, px(12), px(12), fill=color, outline=color)
