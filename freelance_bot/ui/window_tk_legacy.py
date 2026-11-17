"""Windows dashboard for the local services.

The desktop UI only coordinates application services. It does not contain
network or persistence logic, so the same actions remain available through
Telegram and a future richer client.
"""

import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk
from uuid import uuid4

from freelance_bot.core.models import Account, AccountKind, OrderStatus, TelegramSource


class Dashboard:
    def __init__(self, application) -> None:
        self.application = application
        self.root = tk.Tk()
        self.root.title("Freelance Bot - управление")
        self.root.geometry("1000x680")
        self.root.minsize(820, 560)
        self.closed = False
        self.status = tk.StringVar()
        self.metrics = tk.StringVar()
        self.account_name = tk.StringVar()
        self.account_platform = tk.StringVar(value="marketplace")
        self.source_chat = tk.StringVar()
        self.order_query = tk.StringVar()
        self.order_sort = tk.StringVar(value="newest")
        self.show_archive = tk.BooleanVar(value=False)
        self.memory_soft = tk.StringVar(value=str(application.browser.memory_guard.soft_limit_mb))
        self.memory_hard = tk.StringVar(value=str(application.browser.memory_guard.hard_limit_mb))
        self._build()
        self.load_profile()
        if not self.application.telegram.configured:
            self.notebook.select(self.settings_tab)
        self._refresh()

    def _build(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)
        header = ttk.Frame(self.root, padding=(18, 14, 18, 8))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text="Freelance Bot", font=("Segoe UI", 18, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(header, textvariable=self.status).grid(row=1, column=0, sticky="w", pady=(5, 0))
        controls = ttk.Frame(header)
        controls.grid(row=0, column=2, rowspan=2, sticky="e")
        ttk.Button(controls, text="Запустить", command=self.start).pack(side="left", padx=4)
        ttk.Button(controls, text="Пауза", command=self.stop).pack(side="left", padx=4)
        ttk.Button(controls, text="Логи", command=self.toggle_logs).pack(side="left", padx=4)

        self.notebook = ttk.Notebook(self.root)
        self.notebook.grid(row=1, column=0, sticky="nsew", padx=14, pady=(0, 14))
        self.overview_tab = ttk.Frame(self.notebook, padding=18)
        self.sources_tab = ttk.Frame(self.notebook, padding=14)
        self.settings_tab = ttk.Frame(self.notebook, padding=14)
        self.notebook.add(self.overview_tab, text="Главная")
        self.notebook.add(self.sources_tab, text="Telegram")
        self.notebook.add(self.settings_tab, text="Настройки")
        self.orders_tab = ttk.LabelFrame(self.overview_tab, text="Лента заказов", padding=10)
        self.orders_tab.pack(fill="both", expand=True, pady=(12, 0))
        settings_notebook = ttk.Notebook(self.settings_tab)
        settings_notebook.pack(fill="both", expand=True)
        self.accounts_tab = ttk.Frame(settings_notebook, padding=14)
        self.profile_tab = ttk.Frame(settings_notebook, padding=14)
        self.limits_tab = ttk.Frame(settings_notebook, padding=14)
        settings_notebook.add(self.accounts_tab, text="Аккаунты")
        settings_notebook.add(self.profile_tab, text="Профиль и AI")
        settings_notebook.add(self.limits_tab, text="Ресурсы")
        self._build_overview()
        self._build_accounts()
        self._build_sources()
        self._build_orders()
        self._build_profile()
        self._build_settings()
        self.log_frame = ttk.LabelFrame(self.root, text="Журнал работы", padding=8)
        self.log_text = tk.Text(self.log_frame, height=7, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True)

    def _build_overview(self) -> None:
        ttk.Label(self.overview_tab, text="Состояние системы", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Label(self.overview_tab, textvariable=self.metrics, font=("Segoe UI", 11), justify="left").pack(anchor="w", pady=18)
        text = (
            "Как начать:\n"
            "1. Добавьте бота в нужную Telegram-группу или канал.\n"
            "2. На вкладке «Telegram-источники» добавьте ID чата.\n"
            "3. В Telegram используйте /orders для списка заказов.\n"
            "4. Для черновика предложения используйте /draft ID_заказа."
        )
        ttk.Label(self.overview_tab, text=text, justify="left").pack(anchor="w")

    def _build_accounts(self) -> None:
        self.accounts_tab.columnconfigure(0, weight=1)
        self.accounts_tab.rowconfigure(1, weight=1)
        form = ttk.LabelFrame(self.accounts_tab, text="Добавить аккаунт", padding=10)
        form.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(form, text="Название").grid(row=0, column=0, padx=4)
        ttk.Entry(form, textvariable=self.account_name, width=22).grid(row=0, column=1, padx=4)
        ttk.Label(form, text="Площадка").grid(row=0, column=2, padx=4)
        ttk.Entry(form, textvariable=self.account_platform, width=18).grid(row=0, column=3, padx=4)
        ttk.Button(form, text="Добавить", command=self.add_account).grid(row=0, column=4, padx=8)
        columns = ("name", "platform", "kind", "enabled", "attention")
        self.account_tree = ttk.Treeview(self.accounts_tab, columns=columns, show="headings")
        headings = {"name": "Название", "platform": "Площадка", "kind": "Тип", "enabled": "Включён", "attention": "Требует проверки"}
        for column in columns:
            self.account_tree.heading(column, text=headings[column])
            self.account_tree.column(column, width=150, anchor="w")
        self.account_tree.grid(row=1, column=0, sticky="nsew")
        ttk.Button(self.accounts_tab, text="Переключить выбранный аккаунт", command=self.toggle_account).grid(row=2, column=0, sticky="w", pady=10)

    def _build_sources(self) -> None:
        self.sources_tab.columnconfigure(0, weight=1)
        self.sources_tab.rowconfigure(1, weight=1)
        form = ttk.Frame(self.sources_tab)
        form.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(form, text="ID Telegram-чата").pack(side="left")
        ttk.Entry(form, textvariable=self.source_chat, width=28).pack(side="left", padx=8)
        ttk.Button(form, text="Добавить источник", command=self.add_source).pack(side="left")
        self.source_list = tk.Listbox(self.sources_tab, height=10)
        self.source_list.grid(row=1, column=0, sticky="nsew")
        ttk.Button(self.sources_tab, text="Удалить выбранный источник", command=self.remove_source).grid(row=2, column=0, sticky="w", pady=10)

    def _build_orders(self) -> None:
        self.orders_tab.columnconfigure(0, weight=1)
        self.orders_tab.rowconfigure(0, weight=1)
        filters = ttk.Frame(self.orders_tab)
        filters.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(filters, text="Поиск").pack(side="left")
        query = ttk.Entry(filters, textvariable=self.order_query, width=28)
        query.pack(side="left", padx=(5, 10))
        query.bind("<Return>", lambda _event: self.refresh_orders())
        ttk.Label(filters, text="Сортировка").pack(side="left")
        sort = ttk.Combobox(filters, textvariable=self.order_sort, state="readonly", width=18, values=("newest", "budget_desc", "budget_asc", "source"))
        sort.pack(side="left", padx=5)
        sort.bind("<<ComboboxSelected>>", lambda _event: self.refresh_orders())
        ttk.Checkbutton(filters, text="Архив", variable=self.show_archive, command=self.refresh_orders).pack(side="left", padx=8)
        ttk.Button(filters, text="Обновить", command=self.refresh_orders).pack(side="right")

        workspace = ttk.Panedwindow(self.orders_tab, orient="horizontal")
        workspace.grid(row=1, column=0, sticky="nsew")
        self.orders_tab.rowconfigure(1, weight=1)
        list_frame = ttk.Frame(workspace)
        detail_frame = ttk.LabelFrame(workspace, text="Заказ и предложение", padding=8)
        workspace.add(list_frame, weight=3)
        workspace.add(detail_frame, weight=2)
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        columns = ("title", "source", "author", "budget", "published")
        self.order_tree = ttk.Treeview(list_frame, columns=columns, show="headings", selectmode="browse")
        headings = {"title": "Заказ", "source": "Источник", "author": "Автор", "budget": "Бюджет", "published": "Опубликован"}
        for column in columns:
            self.order_tree.heading(column, text=headings[column])
            self.order_tree.column(column, width=130 if column != "title" else 330, anchor="w")
        self.order_tree.grid(row=0, column=0, sticky="nsew")
        self.order_tree.bind("<Double-1>", self.open_order)
        self.order_tree.bind("<<TreeviewSelect>>", self.show_order_details)
        actions = ttk.Frame(list_frame)
        actions.grid(row=1, column=0, sticky="ew", pady=8)
        ttk.Button(actions, text="Открыть оригинал", command=self.open_order).pack(side="left")
        ttk.Button(actions, text="В архив", command=self.archive_selected_order).pack(side="left", padx=8)
        ttk.Label(detail_frame, text="Описание").pack(anchor="w")
        self.order_details = tk.Text(detail_frame, height=10, wrap="word", state="disabled")
        self.order_details.pack(fill="both", expand=True, pady=(3, 10))
        ttk.Label(detail_frame, text="AI-черновик").pack(anchor="w")
        self.draft_text = tk.Text(detail_frame, height=10, wrap="word")
        self.draft_text.pack(fill="both", expand=True, pady=(3, 8))
        draft_actions = ttk.Frame(detail_frame)
        draft_actions.pack(fill="x")
        ttk.Button(draft_actions, text="AI-генерация", command=self.create_draft).pack(side="left")
        ttk.Button(draft_actions, text="Скопировать", command=self.copy_draft).pack(side="left", padx=6)
        ttk.Button(draft_actions, text="Отменить", command=self.clear_draft).pack(side="left")

    def _build_profile(self) -> None:
        self.profile_tab.columnconfigure(1, weight=1)
        ttk.Label(self.profile_tab, text="Навыки через запятую").grid(row=0, column=0, sticky="nw", pady=5)
        self.skills = ttk.Entry(self.profile_tab)
        self.skills.grid(row=0, column=1, sticky="ew", pady=5)
        ttk.Label(self.profile_tab, text="Опыт и специализация").grid(row=1, column=0, sticky="nw", pady=5)
        self.experience = tk.Text(self.profile_tab, height=5, wrap="word")
        self.experience.grid(row=1, column=1, sticky="ew", pady=5)
        ttk.Label(self.profile_tab, text="Примеры вашего стиля").grid(row=2, column=0, sticky="nw", pady=5)
        self.style = tk.Text(self.profile_tab, height=8, wrap="word")
        self.style.grid(row=2, column=1, sticky="ew", pady=5)
        ttk.Label(self.profile_tab, text="Правила для AI").grid(row=3, column=0, sticky="nw", pady=5)
        self.rules = tk.Text(self.profile_tab, height=6, wrap="word")
        self.rules.grid(row=3, column=1, sticky="ew", pady=5)
        ttk.Button(self.profile_tab, text="Сохранить профиль", command=self.save_profile).grid(row=4, column=1, sticky="w", pady=10)

    def _build_settings(self) -> None:
        ttk.Label(self.limits_tab, text="Ограничения памяти процесса", font=("Segoe UI", 12, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))
        ttk.Label(self.limits_tab, text="Мягкий лимит (МБ)").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Entry(self.limits_tab, textvariable=self.memory_soft, width=12).grid(row=1, column=1, sticky="w", pady=5)
        ttk.Label(self.limits_tab, text="Жёсткий лимит (МБ)").grid(row=2, column=0, sticky="w", pady=5)
        ttk.Entry(self.limits_tab, textvariable=self.memory_hard, width=12).grid(row=2, column=1, sticky="w", pady=5)
        ttk.Button(self.limits_tab, text="Применить лимиты", command=self.apply_settings).grid(row=3, column=0, sticky="w", pady=12)
        ttk.Label(self.limits_tab, text="По умолчанию: 4096 МБ мягкий лимит и 5120 МБ жёсткий лимит.", justify="left").grid(row=4, column=0, columnspan=2, sticky="w")

    def start(self) -> None:
        self.application.start_services()
        if self.application.telegram.running:
            self.status.set("Поиск запущен - Telegram подключается")
        else:
            self.status.set("Telegram не настроен — откройте настройки аккаунтов")

    def stop(self) -> None:
        self.application.telegram.running = False
        self.status.set("Пауза - поиск остановлен")

    def add_account(self) -> None:
        name = self.account_name.get().strip()
        platform = self.account_platform.get().strip()
        if not name or not platform:
            messagebox.showwarning("Нужны данные", "Укажите название аккаунта и площадку.")
            return
        account = Account(uuid4().hex, platform, name, AccountKind.MARKETPLACE)
        self.application.accounts.upsert(account, self.application.database)
        self.account_name.set("")
        self.refresh_accounts()

    def toggle_account(self) -> None:
        selected = self.account_tree.selection()
        if not selected:
            return
        tags = self.account_tree.item(selected[0], "tags")
        account_id = tags[0] if tags else ""
        account = self.application.accounts.get(account_id)
        if account:
            self.application.accounts.set_enabled(account_id, not account.enabled)
            self.refresh_accounts()

    def add_source(self) -> None:
        value = self.source_chat.get().strip()
        if value:
            self.application.add_telegram_source(TelegramSource(value))
            self.source_chat.set("")
            self.refresh_sources()

    def remove_source(self) -> None:
        selected = self.source_list.curselection()
        if selected:
            value = self.source_list.get(selected[0]).split(" ", 1)[0]
            self.application.remove_telegram_source(value)
            self.refresh_sources()

    def open_order(self, _event=None) -> None:
        selected = self.order_tree.selection()
        if not selected:
            return
        tags = self.order_tree.item(selected[0], "tags")
        order_id = tags[0] if tags else ""
        order = self.application.database.get_order(order_id)
        if order and order.url:
            webbrowser.open(order.url)

    def create_draft(self) -> None:
        selected = self.order_tree.selection()
        if not selected:
            messagebox.showwarning("Выберите заказ", "Выберите заказ в ленте.")
            return
        tags = self.order_tree.item(selected[0], "tags")
        order_id = tags[0] if tags else ""
        message = self.application._draft_command(0, order_id)
        self.draft_text.delete("1.0", tk.END)
        self.draft_text.insert("1.0", message)

    def show_order_details(self, _event=None) -> None:
        selected = self.order_tree.selection()
        if not selected:
            return
        tags = self.order_tree.item(selected[0], "tags")
        order = self.application.database.get_order(tags[0] if tags else "")
        if order is None:
            return
        source = order.metadata.get("source_name") or order.platform_id
        author = order.metadata.get("author_name") or order.metadata.get("author_username") or "Не указан"
        text = f"Источник: {source}\nАвтор: {author}\nСтатус: {order.status.value}\n\n{order.description}"
        self.order_details.configure(state="normal")
        self.order_details.delete("1.0", tk.END)
        self.order_details.insert("1.0", text)
        self.order_details.configure(state="disabled")

    def archive_selected_order(self) -> None:
        selected = self.order_tree.selection()
        if not selected:
            messagebox.showwarning("Выберите заказ", "Выберите заказ в ленте.")
            return
        tags = self.order_tree.item(selected[0], "tags")
        order_id = tags[0] if tags else ""
        if messagebox.askyesno("Архивировать", "Убрать выбранный заказ из рабочей ленты?"):
            self.application.database.archive_order(order_id)
            self.application.database.log("system", "order_archived", "ok", order_id)
            self.refresh_orders()

    def copy_draft(self) -> None:
        text = self.draft_text.get("1.0", tk.END).strip()
        if text:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.status.set("Черновик скопирован")

    def clear_draft(self) -> None:
        if not self.draft_text.get("1.0", tk.END).strip() or messagebox.askyesno("Отменить черновик", "Закрыть и очистить текст черновика?"):
            self.draft_text.delete("1.0", tk.END)

    def save_profile(self) -> None:
        profile = self.application.proposal_profile
        profile.skills = [item.strip() for item in self.skills.get().split(",") if item.strip()]
        profile.experience = self.experience.get("1.0", tk.END).strip()
        profile.style_examples = [item.strip() for item in self.style.get("1.0", tk.END).split("\n---\n") if item.strip()]
        profile.rules = self.rules.get("1.0", tk.END).strip()
        self.application.profile_store.save(profile)
        messagebox.showinfo("Профиль", "Профиль сохранён для генерации предложений.")

    def load_profile(self) -> None:
        profile = self.application.proposal_profile
        self.skills.insert(0, ", ".join(profile.skills))
        self.experience.insert("1.0", profile.experience)
        self.style.insert("1.0", "\n---\n".join(profile.style_examples))
        self.rules.insert("1.0", profile.rules)

    def apply_settings(self) -> None:
        try:
            soft, hard = int(self.memory_soft.get()), int(self.memory_hard.get())
            if not 256 <= soft < hard <= 5120:
                raise ValueError
            self.application.browser.memory_guard.soft_limit_mb = soft
            self.application.browser.memory_guard.hard_limit_mb = hard
            messagebox.showinfo("Настройки", "Лимиты памяти применены.")
        except ValueError:
            messagebox.showerror("Ошибка", "Введите лимиты от 256 до 5120 МБ, мягкий лимит должен быть меньше жёсткого.")

    def toggle_logs(self) -> None:
        if self.log_frame.winfo_manager():
            self.log_frame.grid_forget()
        else:
            self.log_frame.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 10))
            self.refresh_logs()

    def refresh_logs(self) -> None:
        if not self.log_frame.winfo_manager():
            return
        lines = [f"{row['created_at']} | {row['status']} | {row['action']} | {row['details']}" for row in self.application.database.list_logs(100)]
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", tk.END)
        self.log_text.insert("1.0", "\n".join(lines))
        self.log_text.configure(state="disabled")

    def refresh_accounts(self) -> None:
        for item in self.account_tree.get_children():
            self.account_tree.delete(item)
        for account in self.application.accounts.all():
            item = self.account_tree.insert("", "end", values=(account.name, account.platform_id, account.kind.value, "да" if account.enabled else "нет", "да" if account.requires_attention else "нет"))
            self.account_tree.item(item, tags=(account.id,))

    def refresh_sources(self) -> None:
        self.source_list.delete(0, tk.END)
        for source in self.application.telegram_sources.values():
            suffix = "" if source.enabled else " (выключен)"
            self.source_list.insert(tk.END, f"{source.chat_id}{suffix}")

    def refresh_orders(self) -> None:
        for item in self.order_tree.get_children():
            self.order_tree.delete(item)
        status = OrderStatus.ARCHIVED if self.show_archive.get() else OrderStatus.ACTIVE
        for order in self.application.database.list_orders(100, status=status, query=self.order_query.get(), sort=self.order_sort.get()):
            source = str(order.metadata.get("source_name") or order.platform_id)
            author = str(order.metadata.get("author_name") or order.metadata.get("author_username") or "Не указано")
            budget = f"{order.budget:g} {order.currency}" if order.budget is not None else "Не указан"
            published = order.published_at.astimezone().strftime("%d.%m %H:%M") if order.published_at else "Не указано"
            item = self.order_tree.insert("", "end", values=(order.title, source, author, budget, published))
            self.order_tree.item(item, tags=(order.id,))

    def _refresh(self) -> None:
        if self.closed:
            return
        running = self.application.telegram.running
        if not self.application.telegram.configured:
            self.status.set("Telegram не настроен — добавьте токен и владельца")
        elif self.application.telegram_transport.connected:
            self.status.set("Работает — Telegram подключён" if running else "Пауза — поиск остановлен")
        else:
            self.status.set("Telegram подключается" if running else "Пауза — поиск остановлен")
        self.metrics.set(
            f"Статус: {'работает' if running else 'пауза'}\n"
            f"Парсинг: {self.application.parsing_state.value}\n"
            f"Автоматизация: {self.application.automation_state.value}\n"
            f"Telegram-постинг: {self.application.posting_state.value}\n"
            f"Найдено заказов: {self.application.database.count('orders')}\n"
            f"Сохранено аккаунтов: {len(self.application.accounts.all())}\n"
            f"Активных аккаунтов: {len(self.application.accounts.active())}/{self.application.accounts.max_active}\n"
            f"Память процесса: {self.application.browser.memory_guard.rss_mb():.0f} МБ"
        )
        self.refresh_accounts()
        self.refresh_sources()
        self.refresh_orders()
        self.refresh_logs()
        self.root.after(1500, self._refresh)

    def run(self) -> None:
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.mainloop()

    def _close(self) -> None:
        self.closed = True
        self.application.shutdown()
        self.root.destroy()
