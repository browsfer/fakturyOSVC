from __future__ import annotations

# Jednoplikowa aplikacja V5: faktury, podatki/składki (w tym zmiana vedlejší → hlavní) i DPH/VIES. Przy pierwszym uruchomieniu automatycznie doinstaluje
# Flask i ReportLab, jeżeli nie są jeszcze dostępne w tym Pythonie.
import importlib.util
import subprocess
import sys
import calendar
import csv
import xml.etree.ElementTree as ET


def _ensure_dependencies() -> None:
    required = []
    if importlib.util.find_spec("flask") is None:
        required.append("Flask>=3.0,<4")
    if importlib.util.find_spec("reportlab") is None:
        required.append("reportlab>=4.0,<5")
    if not required:
        return
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", *required])
    except Exception:
        try:
            subprocess.check_call([sys.executable, "-m", "ensurepip", "--upgrade"])
            subprocess.check_call([sys.executable, "-m", "pip", "install", *required])
        except Exception as exc:
            print("Nie udało się zainstalować wymaganych bibliotek:", ", ".join(required))
            print("Uruchom ręcznie: python -m pip install flask reportlab")
            input("Naciśnij Enter, aby zakończyć...")
            raise SystemExit(1) from exc


_ensure_dependencies()

import os
import secrets

# V7.2 FREE: tryb web/PWA bez płatnego persistent disk.
APP_PASSWORD = os.environ.get("APP_PASSWORD", "").strip()
SECRET_KEY = os.environ.get("SECRET_KEY", "").strip() or secrets.token_hex(32)
CLOUD_MODE = bool(os.environ.get("RENDER") or os.environ.get("APP_CLOUD_MODE"))

# Trwałość danych w darmowym wariancie:
# Render Free = aplikacja, Supabase Storage Free = prywatna kopia pliku SQLite.
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
SUPABASE_BUCKET = os.environ.get("SUPABASE_BUCKET", "faktury-osvc").strip() or "faktury-osvc"
SUPABASE_DB_OBJECT = os.environ.get("SUPABASE_DB_OBJECT", "data/invoice_app.db").strip() or "data/invoice_app.db"
REMOTE_DB_ENABLED = bool(SUPABASE_URL and SUPABASE_SERVICE_KEY)
SUPABASE_KEY_KIND = (
    "new_secret" if SUPABASE_SERVICE_KEY.startswith("sb_secret_")
    else "legacy_service_role" if SUPABASE_SERVICE_KEY.startswith("eyJ")
    else "unknown" if SUPABASE_SERVICE_KEY
    else "missing"
)

import shutil
import tempfile
import re
import sqlite3
import threading
import unicodedata
import webbrowser
import hmac
import urllib.request
import urllib.error
import urllib.parse
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, ROUND_CEILING
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from flask import (
    Flask,
    Response,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
    session,
)
from jinja2 import DictLoader

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    KeepTogether,
    LongTable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


# Szablony, CSS i JavaScript są osadzone w tym jednym pliku.
EMBEDDED_TEMPLATES = {'404.html': '{% extends "base.html" %}\n'
             '{% block title %}Nie znaleziono - Faktury OSVČ{% endblock %}\n'
             '{% block content %}<div class="empty-state page-empty"><h1>404</h1><h2>Nie znaleziono strony</h2><a '
             'class="btn btn-primary" href="{{ url_for(\'dashboard\') }}">Wróć na start</a></div>{% endblock %}\n',
 'base.html': '<!doctype html>\n'
              '<html lang="pl">\n'
              '<head>\n'
              '    <meta charset="utf-8">\n'
              '    <meta name="viewport" content="width=device-width, initial-scale=1">\n'
              '    <title>{% block title %}Faktury OSVČ{% endblock %}</title>\n'
              '    <link rel="stylesheet" href="{{ url_for(\'static\', filename=\'style.css\') }}">\n'
              '</head>\n'
              '<body>\n'
              '<header class="topbar">\n'
              '    <div class="topbar-inner">\n'
              '        <a class="brand" href="{{ url_for(\'dashboard\') }}">Faktury OSVČ</a>\n'
              '        <nav class="nav">\n'
              '            <a href="{{ url_for(\'dashboard\') }}">Start</a>\n'
              '            <a href="{{ url_for(\'invoices_list\') }}">Faktury</a>\n'
              '            <a href="{{ url_for(\'contractors_list\') }}">Kontrahenci</a>\n'
              '            <a href="{{ url_for(\'expenses_page\') }}">Koszty</a>\n'
              '            <a href="{{ url_for(\'dph_dashboard\') }}">DPH / UE</a>\n'
              '            <a href="{{ url_for(\'company_edit\') }}">Moja firma</a>\n'
              '            <a href="{{ url_for(\'tools_page\') }}">Narzędzia</a>\n'
              '        </nav>\n'
              '        <a class="btn btn-primary btn-small" href="{{ url_for(\'invoice_new\') }}">+ Nowa faktura</a>\n'
              '    </div>\n'
              '</header>\n'
              '\n'
              '<main class="container">\n'
              '    {% with messages = get_flashed_messages(with_categories=true) %}\n'
              '        {% if messages %}\n'
              '            <div class="flash-stack">\n'
              '                {% for category, message in messages %}\n'
              '                    <div class="flash flash-{{ category }}">{{ message }}</div>\n'
              '                {% endfor %}\n'
              '            </div>\n'
              '        {% endif %}\n'
              '    {% endwith %}\n'
              '    {% block content %}{% endblock %}\n'
              '</main>\n'
              '\n'
              '<footer class="footer">\n'
              '    <div class="container footer-inner">\n'
              '        <span>Dane są przechowywane lokalnie. Aktualizacja programu nie usuwa kontrahentów, faktur ani '
              'kosztów.</span>\n'
              '        <a href="{{ url_for(\'backup_database\') }}">Pobierz kopię bazy</a>\n'
              '    </div>\n'
              '</footer>\n'
              '<script src="{{ url_for(\'static\', filename=\'app.js\') }}"></script>\n'
              '{% block scripts %}{% endblock %}\n'
              '</body>\n'
              '</html>\n',
 'company_form.html': '{% extends "base.html" %}\n'
                      '{% block title %}Moja firma - Faktury OSVČ{% endblock %}\n'
                      '{% block content %}\n'
                      '<div class="page-header">\n'
                      '    <div><h1>Dane własnej firmy</h1><p class="muted">Te informacje będą drukowane na fakturach '
                      'PDF.</p></div>\n'
                      '</div>\n'
                      '<form method="post" class="panel form-panel">\n'
                      '    <h2>Dane identyfikacyjne</h2>\n'
                      '    <div class="form-grid two">\n'
                      '        <label class="field span-2"><span>Nazwa / imię i nazwisko *</span><input name="name" '
                      'value="{{ company.name }}" required></label>\n'
                      '        <label class="field"><span>IČO / numer firmy</span><input name="company_id" value="{{ '
                      'company.company_id }}"></label>\n'
                      '        <label class="field"><span>DIČ / VAT ID</span><input name="vat_id" value="{{ '
                      'company.vat_id }}"></label>\n'
                      '        <label class="field span-2"><span>Ulica i numer</span><input name="address" value="{{ '
                      'company.address }}"></label>\n'
                      '        <label class="field"><span>Kod pocztowy</span><input name="postal_code" value="{{ '
                      'company.postal_code }}"></label>\n'
                      '        <label class="field"><span>Miasto</span><input name="city" value="{{ company.city '
                      '}}"></label>\n'
                      '        <label class="field span-2"><span>Kraj</span><input name="country" value="{{ '
                      'company.country }}"></label>\n'
                      '        <label class="field"><span>E-mail</span><input type="email" name="email" value="{{ '
                      'company.email }}"></label>\n'
                      '        <label class="field"><span>Telefon</span><input name="phone" value="{{ company.phone '
                      '}}"></label>\n'
                      '    </div>\n'
                      '\n'
                      '    <hr>\n'
                      '    <h2>Dane bankowe</h2>\n'
                      '    <div class="form-grid two">\n'
                      '        <label class="field"><span>Nazwa banku</span><input name="bank_name" value="{{ '
                      'company.bank_name }}"></label>\n'
                      '        <label class="field"><span>Numer rachunku lokalnego</span><input name="bank_account" '
                      'value="{{ company.bank_account }}"></label>\n'
                      '        <label class="field"><span>IBAN</span><input name="iban" value="{{ company.iban '
                      '}}"></label>\n'
                      '        <label class="field"><span>BIC / SWIFT</span><input name="bic" value="{{ company.bic '
                      '}}"></label>\n'
                      '    </div>\n'
                      '\n'
                      '    <hr>\n'
                      '    <h2>Ustawienia faktur</h2>\n'
                      '    <div class="form-grid three">\n'
                      '        <label class="field"><span>Domyślna waluta</span><input name="default_currency" '
                      'value="{{ company.default_currency }}" maxlength="6"></label>\n'
                      '        <label class="field"><span>Termin płatności (dni)</span><input type="number" min="0" '
                      'max="365" name="default_due_days" value="{{ company.default_due_days }}"></label>\n'
                      '        <label class="field"><span>Prefiks numeru</span><input name="invoice_prefix" value="{{ '
                      'company.invoice_prefix }}" maxlength="12"></label>\n'
                      '        <label class="field"><span>Domyślny język PDF</span><select name="default_language">{% '
                      'for code, label in LANGUAGES.items() %}<option value="{{ code }}" {% if '
                      'company.default_language == code %}selected{% endif %}>{{ label }}</option>{% endfor '
                      '%}</select></label>\n'
                      '        <label class="field"><span>Domyślny tryb VAT</span><select name="default_tax_mode">{% '
                      'for code, label in TAX_MODES.items() %}<option value="{{ code }}" {% if '
                      'company.default_tax_mode == code %}selected{% endif %}>{{ label }}</option>{% endfor '
                      '%}</select></label>\n'
                      '        <div></div>\n'
                      '        <label class="field span-3"><span>Domyślna adnotacja reverse charge</span><textarea '
                      'name="default_reverse_charge_note" rows="3">{{ company.default_reverse_charge_note '
                      '}}</textarea></label>\n'
                      '    </div>\n'
                      '    <hr>\n'
                      '    <h2>Ustawienia Souhrnné hlášení VIES</h2>\n'
                      '    <div class="callout info">Te dane służą do przygotowania pliku XML do MOJE daně. Kod 451 oznacza '
                      'Finanční úřad pro hlavní město Prahu, a kod 2002 jego Územní pracoviště pro Prahu 2. Dla osoby '
                      'fizycznej właściwość wynika z miejsca zamieszkania, a nie z adresu siedziby OSVČ; dlatego Praha 2 '
                      'pozostaje prawidłowa przy Twoim mieszkaniu na Mánesovej.</div>\n'
                      '    <div class="form-grid two">\n'
                      '        <label class="field"><span>Imię / imiona podatnika</span><input name="tax_first_name" '
                      'value="{{ company.tax_first_name }}"></label>\n'
                      '        <label class="field"><span>Nazwisko podatnika</span><input name="tax_last_name" '
                      'value="{{ company.tax_last_name }}"></label>\n'
                      '        <label class="field"><span>Kod finančního úřadu (c_ufo)</span><input '
                      'name="tax_office_code" value="{{ company.tax_office_code }}" maxlength="3"></label>\n'
                      '        <label class="field"><span>Kod územního pracoviště (c_pracufo)</span><input '
                      'name="tax_branch_code" value="{{ company.tax_branch_code }}" maxlength="4"></label>\n'
                      '    </div>\n'
                      '    <div class="form-actions"><button class="btn btn-primary" type="submit">Zapisz dane '
                      'firmy</button></div>\n'
                      '</form>\n'
                      '{% endblock %}\n',
 'contractor_form.html': '{% extends "base.html" %}\n'
                         "{% block title %}{{ 'Edytuj kontrahenta' if is_edit else 'Nowy kontrahent' }} - Faktury "
                         'OSVČ{% endblock %}\n'
                         '{% block content %}\n'
                         '<div class="page-header"><div><h1>{{ \'Edytuj kontrahenta\' if is_edit else \'Nowy '
                         'kontrahent\' }}</h1><p class="muted">Dane zostaną użyte w sekcji nabywcy na '
                         'fakturze.</p></div></div>\n'
                         '<form method="post" class="panel form-panel">\n'
                         '    <div class="form-grid two">\n'
                         '        <label class="field span-2"><span>Nazwa firmy *</span><input name="name" value="{{ '
                         'contractor.name }}" required autofocus></label>\n'
                         '        <label class="field"><span>VAT ID / UID</span><input name="vat_id" value="{{ '
                         'contractor.vat_id }}" placeholder="ATU12345678"></label>\n'
                         '        <label class="field"><span>Numer firmy</span><input name="company_id" value="{{ '
                         'contractor.company_id }}"></label>\n'
                         '        <label class="field span-2"><span>Ulica i numer</span><input name="address" '
                         'value="{{ contractor.address }}"></label>\n'
                         '        <label class="field"><span>Kod pocztowy</span><input name="postal_code" value="{{ '
                         'contractor.postal_code }}"></label>\n'
                         '        <label class="field"><span>Miasto</span><input name="city" value="{{ contractor.city '
                         '}}"></label>\n'
                         '        <label class="field span-2"><span>Kraj</span><input name="country" value="{{ '
                         'contractor.country }}"></label>\n'
                         '        <label class="field"><span>E-mail</span><input type="email" name="email" value="{{ '
                         'contractor.email }}"></label>\n'
                         '        <label class="field"><span>Telefon</span><input name="phone" value="{{ '
                         'contractor.phone }}"></label>\n'
                         '        <label class="field span-2"><span>Notatki wewnętrzne</span><textarea name="notes" '
                         'rows="3">{{ contractor.notes }}</textarea></label>\n'
                         '    </div>\n'
                         '    <div class="form-actions">\n'
                         '        <a class="btn btn-ghost" href="{{ url_for(\'contractors_list\') }}">Anuluj</a>\n'
                         '        <button class="btn btn-primary" type="submit">{{ \'Zapisz zmiany\' if is_edit else '
                         "'Dodaj kontrahenta' }}</button>\n"
                         '    </div>\n'
                         '</form>\n'
                         '{% endblock %}\n',
 'contractors_list.html': '{% extends "base.html" %}\n'
                          '{% block title %}Kontrahenci - Faktury OSVČ{% endblock %}\n'
                          '{% block content %}\n'
                          '<div class="page-header">\n'
                          '    <div><h1>Kontrahenci</h1><p class="muted">Dodaj firmę raz, a później wybieraj ją przy '
                          'wystawianiu faktury.</p></div>\n'
                          '    <a class="btn btn-primary" href="{{ url_for(\'contractor_new\') }}">+ Dodaj '
                          'kontrahenta</a>\n'
                          '</div>\n'
                          '<form class="toolbar" method="get">\n'
                          '    <input type="search" name="q" value="{{ q }}" placeholder="Szukaj po nazwie, VAT ID lub '
                          'mieście">\n'
                          '    <button class="btn btn-light" type="submit">Szukaj</button>\n'
                          '    {% if q %}<a class="btn btn-ghost" href="{{ url_for(\'contractors_list\') '
                          '}}">Wyczyść</a>{% endif %}\n'
                          '</form>\n'
                          '<section class="panel">\n'
                          '{% if contractors %}\n'
                          '<div class="table-wrap">\n'
                          '<table>\n'
                          '    <thead><tr><th>Nazwa</th><th>Miasto / kraj</th><th>VAT '
                          'ID</th><th>E-mail</th><th></th></tr></thead>\n'
                          '    <tbody>\n'
                          '    {% for contractor in contractors %}\n'
                          '    <tr>\n'
                          '        <td><strong>{{ contractor.name }}</strong><div class="muted small">{{ '
                          'contractor.address }}</div></td>\n'
                          '        <td>{{ contractor.city }}{% if contractor.country %}, {{ contractor.country }}{% '
                          'endif %}</td>\n'
                          '        <td>{{ contractor.vat_id }}</td>\n'
                          '        <td>{{ contractor.email }}</td>\n'
                          '        <td class="actions">\n'
                          '            <a class="btn btn-light btn-small" href="{{ url_for(\'invoice_new\', '
                          'contractor_id=contractor.id) }}">Faktura</a>\n'
                          '            <a class="btn btn-light btn-small" href="{{ url_for(\'contractor_edit\', '
                          'contractor_id=contractor.id) }}">Edytuj</a>\n'
                          '            <form method="post" action="{{ url_for(\'contractor_delete\', '
                          'contractor_id=contractor.id) }}" class="inline-form" onsubmit="return confirm(\'Usunąć tego '
                          'kontrahenta?\')"><button class="btn btn-danger btn-small" '
                          'type="submit">Usuń</button></form>\n'
                          '        </td>\n'
                          '    </tr>\n'
                          '    {% endfor %}\n'
                          '    </tbody>\n'
                          '</table>\n'
                          '</div>\n'
                          '{% else %}\n'
                          '<div class="empty-state"><h3>Brak kontrahentów</h3><p>Dodaj pierwszą firmę, aby wystawić '
                          'dla niej fakturę.</p><a class="btn btn-primary" href="{{ url_for(\'contractor_new\') '
                          '}}">Dodaj kontrahenta</a></div>\n'
                          '{% endif %}\n'
                          '</section>\n'
                          '{% endblock %}\n',
 'dashboard.html': '{% extends "base.html" %}\n'
                   '{% block title %}Start - Faktury OSVČ{% endblock %}\n'
                   '{% block content %}\n'
                   '<div class="page-header">\n'
                   '    <div>\n'
                   '        <h1>Panel działalności</h1>\n'
                   '        <p class="muted">{{ company.name }} · IČO {{ company.company_id }} · DIČ {{ company.vat_id '
                   '}}</p>\n'
                   '    </div>\n'
                   '    <a class="btn btn-primary" href="{{ url_for(\'invoice_new\') }}">Wystaw fakturę</a>\n'
                   '</div>\n'
                   '\n'
                   '{% if dph_reminder %}\n'
                   '<div class="callout warning">\n'
                   '    <strong>DPH / UE:</strong> masz {{ dph_reminder.invoice_count }} fakturę/faktury do '
                   'sprawdzenia za {{ dph_reminder.period_label }}.\n'
                   '    Podstawowy termin: <strong>{{ dph_reminder.due_date|date_pl }}</strong>.\n'
                   '    <a href="{{ url_for(\'dph_dashboard\', year=dph_reminder.year, month=dph_reminder.month) '
                   '}}">Otwórz asystenta DPH</a>.\n'
                   '</div>\n'
                   '{% endif %}\n'
                   '\n'
                   '<div class="stats-grid dashboard-stats">\n'
                   '    <div class="stat-card"><span class="stat-value compact-value">{{ '
                   'month_summary.revenue|money(\'CZK\') }}</span><span class="stat-label">Przychód w tym '
                   'miesiącu</span></div>\n'
                   '    <div class="stat-card"><span class="stat-value compact-value">{{ '
                   'month_summary.costs|money(\'CZK\') }}</span><span class="stat-label">Koszty w tym '
                   'miesiącu</span></div>\n'
                   '    <div class="stat-card"><span class="stat-value compact-value">{{ '
                   'month_summary.result|money(\'CZK\') }}</span><span class="stat-label">Wynik przed '
                   'podatkami</span></div>\n'
                   '    <div class="stat-card"><span class="stat-value">{{ counts.unpaid }}</span><span '
                   'class="stat-label">Nieopłacone faktury</span></div>\n'
                   '    <div class="stat-card"><span class="stat-value">{{ counts.invoices }}</span><span '
                   'class="stat-label">Wszystkie faktury</span></div>\n'
                   '    <div class="stat-card"><span class="stat-value">{{ counts.contractors }}</span><span '
                   'class="stat-label">Kontrahenci</span></div>\n'
                   '</div>\n'
                   '\n'
                   '<section class="panel">\n'
                   '    <div class="panel-header">\n'
                   '        <h2>Ostatnie faktury</h2>\n'
                   '        <a href="{{ url_for(\'invoices_list\') }}">Pokaż wszystkie</a>\n'
                   '    </div>\n'
                   '    {% if recent %}\n'
                   '    <div class="table-wrap">\n'
                   '        <table>\n'
                   '            '
                   '<thead><tr><th>Numer</th><th>Kontrahent</th><th>Data</th><th>Kwota</th><th>Status</th><th></th></tr></thead>\n'
                   '            <tbody>\n'
                   '            {% for invoice in recent %}\n'
                   '                <tr>\n'
                   '                    <td><a href="{{ url_for(\'invoice_view\', invoice_id=invoice.id) '
                   '}}"><strong>{{ invoice.invoice_number }}</strong></a></td>\n'
                   '                    <td>{{ invoice.contractor_name }}</td>\n'
                   '                    <td>{{ invoice.issue_date|date_pl }}</td>\n'
                   '                    <td>{{ invoice.total|money(invoice.currency) }}</td>\n'
                   '                    <td><span class="badge badge-{{ invoice.status }}">{{ \'Opłacona\' if '
                   "invoice.status == 'paid' else 'Nieopłacona' }}</span></td>\n"
                   '                    <td class="actions"><a class="btn btn-light btn-small" href="{{ '
                   'url_for(\'invoice_pdf\', invoice_id=invoice.id) }}">PDF</a></td>\n'
                   '                </tr>\n'
                   '            {% endfor %}\n'
                   '            </tbody>\n'
                   '        </table>\n'
                   '    </div>\n'
                   '    {% else %}\n'
                   '        <div class="empty-state"><h3>Nie masz jeszcze faktur</h3><p>Dodaj kontrahenta, a następnie '
                   'wystaw pierwszą fakturę.</p></div>\n'
                   '    {% endif %}\n'
                   '</section>\n'
                   '{% endblock %}\n',
 'dph_dashboard.html': '{% extends "base.html" %}\n'
                       '{% block title %}DPH / UE - Faktury OSVČ{% endblock %}\n'
                       '{% block content %}\n'
                       '<div class="page-header"><div><h1>Asystent DPH / UE</h1><p class="muted">Przygotowanie '
                       'Souhrnné hlášení VIES dla usług B2B w UE.</p></div><div class="button-row"><a class="btn '
                       'btn-light" target="_blank" rel="noopener" href="{{ vies_url }}">Sprawdź VAT ID w VIES</a><a '
                       'class="btn btn-primary" target="_blank" rel="noopener" href="{{ moje_dane_url }}">Otwórz MOJE '
                       'daně</a></div></div>\n'
                       '<div class="callout info"><strong>Proces:</strong> sprawdź dane, pobierz XML, otwórz MOJE '
                       'daně, wybierz wczytanie pliku, wykonaj kontrolę i dopiero wtedy wyślij. Program nie loguje się '
                       'do urzędu i nie wysyła zgłoszenia automatycznie.</div>\n'
                       '<div class="callout warning"><strong>Zakres automatyki:</strong> moduł zakłada zwykłą usługę '
                       'B2B w UE według § 9 odst. 1. Usługi związane z nieruchomością lub budową, zaliczki, korekty i '
                       'inne szczególne przypadki wymagają ręcznego sprawdzenia.</div>\n'
                       '<form class="toolbar" method="get"><select name="month">{% for value, label in MONTHS.items() '
                       '%}<option value="{{ value }}" {% if selected_month == value %}selected{% endif %}>{{ label '
                       '}}</option>{% endfor %}</select><input type="number" name="year" value="{{ selected_year }}" '
                       'min="2020" max="2100"><button class="btn btn-light" type="submit">Pokaż okres</button></form>\n'
                       '<div class="stats-grid dashboard-stats"><div class="stat-card"><span class="stat-value">{{ '
                       'report.invoice_count }}</span><span class="stat-label">Faktury / transakcje</span></div><div '
                       'class="stat-card"><span class="stat-value compact-value">{{ report.total_czk|money(\'CZK\') '
                       '}}</span><span class="stat-label">Wartość do SH</span></div><div class="stat-card"><span '
                       'class="stat-value compact-value">{{ due_date|date_pl }}</span><span '
                       'class="stat-label">Podstawowy termin</span></div><div class="stat-card"><span '
                       'class="stat-value status-value">{{ \'Wysłane\' if filing and filing.status == \'filed\' else '
                       '\'Do wysłania\' }}</span><span class="stat-label">Status {{ period_label '
                       '}}</span></div></div>\n'
                       '{% if report.warnings %}<div class="callout warning"><strong>Przed eksportem '
                       'popraw:</strong><ul class="compact-list">{% for warning in report.warnings %}<li>{{ warning '
                       '}}</li>{% endfor %}</ul></div>{% endif %}\n'
                       '{% if report.rows %}<section class="panel"><div class="panel-header"><h2>Wiersze Souhrnné '
                       'hlášení</h2><div class="button-row"><a class="btn btn-light" href="{{ '
                       'url_for(\'dph_export_csv\', year=selected_year, month=selected_month) }}">CSV kontrolny</a><a '
                       'class="btn btn-primary {% if report.block_export %}disabled-link{% endif %}" {% if not '
                       'report.block_export %}href="{{ url_for(\'dph_export_shv\', year=selected_year, '
                       'month=selected_month) }}"{% endif %}>Pobierz XML do MOJE daně</a></div></div><div '
                       'class="table-wrap"><table><thead><tr><th>Kraj</th><th>VAT ID bez prefiksu</th><th>Kod '
                       'plnění</th><th>Liczba transakcji</th><th>Wartość CZK</th></tr></thead><tbody>{% for row in '
                       'report.rows %}<tr><td>{{ row.country_code }}</td><td><strong>{{ row.vat_number '
                       "}}</strong></td><td>3</td><td>{{ row.count }}</td><td>{{ row.value_czk|money('CZK') "
                       '}}</td></tr>{% endfor %}</tbody></table></div></section>\n'
                       '<section class="panel"><div class="panel-header"><h2>Faktury źródłowe</h2></div><div '
                       'class="table-wrap"><table><thead><tr><th>Faktura</th><th>Kontrahent</th><th>Data '
                       'usługi</th><th>Kwota</th><th>Kurs</th><th>W CZK</th></tr></thead><tbody>{% for invoice in '
                       'report.invoices %}<tr><td><a href="{{ url_for(\'invoice_view\', invoice_id=invoice.id) '
                       '}}"><strong>{{ invoice.invoice_number }}</strong></a></td><td>{{ invoice.contractor_name '
                       '}}<br><span class="small muted">{{ invoice.contractor_vat_id }}</span></td><td>{{ '
                       'invoice.supply_date|date_pl }}</td><td>{{ invoice.total_net|money(invoice.currency) '
                       "}}</td><td>{{ invoice.rate or 'brak' }}</td><td>{{ invoice.value_czk|money('CZK') if "
                       "invoice.value_czk is not none else '—' }}</td></tr>{% endfor "
                       '%}</tbody></table></div></section>{% else %}<section class="panel"><div '
                       'class="empty-state"><h3>Brak faktur do SH VIES za {{ period_label }}</h3><p>Zerowego '
                       'souhrnného hlášení nie przygotowuje się.</p></div></section>{% endif %}\n'
                       '{% if filing and filing.status == \'filed\' %}<section class="panel"><div '
                       'class="panel-header"><h2>Oznaczone jako wysłane</h2><span class="badge badge-paid">{{ '
                       'filing.filed_date|date_pl }}</span></div><p>Numer potwierdzenia: <strong>{{ '
                       'filing.confirmation_number or \'nie podano\' }}</strong></p><form method="post" action="{{ '
                       'url_for(\'dph_reopen_filing\') }}"><input type="hidden" name="year" value="{{ selected_year '
                       '}}"><input type="hidden" name="month" value="{{ selected_month }}"><button class="btn '
                       'btn-light" type="submit">Oznacz jako niewysłane</button></form></section>{% else %}<section '
                       'class="panel"><div class="panel-header"><h2>Po wysłaniu w MOJE daně</h2></div><form '
                       'method="post" action="{{ url_for(\'dph_mark_filed\') }}" class="form-panel '
                       'compact-form"><input type="hidden" name="year" value="{{ selected_year }}"><input '
                       'type="hidden" name="month" value="{{ selected_month }}"><div class="form-grid two"><label '
                       'class="field"><span>Data wysłania</span><input type="date" name="filed_date" value="{{ today '
                       '}}" required></label><label class="field"><span>ID podání / numer potwierdzenia</span><input '
                       'name="confirmation_number"></label><label class="field span-2"><span>Uwagi</span><textarea '
                       'name="notes" rows="2"></textarea></label></div><div class="form-actions"><button class="btn '
                       'btn-primary" type="submit">Oznacz jako wysłane</button></div></form></section>{% endif %}\n'
                       '{% if foreign_expenses %}<section class="panel"><div class="panel-header"><h2>Koszty '
                       'wymagające sprawdzenia DPH</h2></div><div '
                       'class="table-wrap"><table><thead><tr><th>Data</th><th>Opis</th><th>Dostawca</th><th>Wartość</th></tr></thead><tbody>{% '
                       'for expense in foreign_expenses %}<tr><td>{{ expense.expense_date|date_pl }}</td><td>{{ '
                       "expense.description }}</td><td>{{ expense.supplier or '—' }}</td><td>{{ "
                       "expense.amount_czk|money('CZK') }}</td></tr>{% endfor %}</tbody></table></div><p "
                       'class="small muted">Zakup zagranicznej usługi może wymagać czeskiego přiznání k DPH. Ten moduł '
                       'przygotowuje obecnie SH VIES dla sprzedaży; koszt oznacza tylko jako '
                       'ostrzeżenie.</p></section>{% endif %}\n'
                       '<p class="small muted">XML jest przygotowywany według struktury DPHSHV 02.01.04. Portal MOJE '
                       'daně pozostaje ostatecznym miejscem kontroli i wysłania.</p>\n'
                       '{% endblock %}\n',
 'expense_form.html': '{% extends "base.html" %}\n'
                      "{% block title %}{{ 'Edytuj koszt' if is_edit else 'Nowy koszt' }} - Faktury OSVČ{% endblock "
                      '%}\n'
                      '{% block content %}\n'
                      '<div class="page-header"><div><h1>{{ \'Edytuj koszt\' if is_edit else \'Nowy koszt\' }}</h1><p '
                      'class="muted">Wprowadź rzeczywiście poniesiony wydatek związany z '
                      'działalnością.</p></div></div>\n'
                      '<form method="post" class="panel form-panel expense-form">\n'
                      '<div class="form-grid three">\n'
                      '<label class="field"><span>Data *</span><input type="date" name="expense_date" value="{{ '
                      'expense.expense_date }}" required></label>\n'
                      '<label class="field"><span>Dostawca</span><input name="supplier" value="{{ expense.supplier '
                      '}}"></label>\n'
                      '<label class="field"><span>Numer dokumentu</span><input name="document_number" value="{{ '
                      'expense.document_number }}"></label>\n'
                      '<label class="field span-2"><span>Opis kosztu *</span><input name="description" value="{{ '
                      'expense.description }}" required></label>\n'
                      '<label class="field"><span>Kategoria</span><select name="category">{% for code, label in '
                      'EXPENSE_CATEGORIES.items() %}<option value="{{ code }}" {% if expense.category == code '
                      '%}selected{% endif %}>{{ label }}</option>{% endfor %}</select></label>\n'
                      '<label class="field"><span>Kwota *</span><input id="expense-amount" type="number" step="0.01" '
                      'min="0" name="amount" value="{{ expense.amount }}" required></label>\n'
                      '<label class="field"><span>Waluta</span><input id="expense-currency" name="currency" value="{{ '
                      'expense.currency }}" maxlength="6"></label>\n'
                      '<label class="field"><span>Kurs do CZK</span><input id="expense-rate" type="number" '
                      'step="0.0001" min="0" name="czk_rate" value="{{ expense.czk_rate }}"></label>\n'
                      '<label class="field"><span>Część związana z działalnością (%)</span><input type="number" '
                      'min="0" max="100" step="1" name="business_percent" value="{{ expense.business_percent '
                      '}}"></label>\n'
                      '<label class="field"><span>Sposób płatności</span><select name="payment_method">{% for code, '
                      'label in PAYMENT_METHODS.items() %}<option value="{{ code }}" {% if expense.payment_method == '
                      'code %}selected{% endif %}>{{ label }}</option>{% endfor %}</select></label>\n'
                      '<label class="field"><span>Klasyfikacja DPH</span><select name="dph_obligation">{% for code, '
                      'label in EXPENSE_DPH_OBLIGATIONS.items() %}<option value="{{ code }}" {% if '
                      'expense.dph_obligation == code %}selected{% endif %}>{{ label }}</option>{% endfor '
                      '%}</select></label>\n'
                      '<label class="field"><span>VAT ID dostawcy</span><input name="supplier_vat_id" value="{{ '
                      'expense.supplier_vat_id }}"></label>\n'
                      '<label class="field"><span>Kraj dostawcy</span><input name="country" value="{{ expense.country '
                      '}}"></label>\n'
                      '<label class="field span-3"><span>Uwagi</span><textarea name="notes" rows="3">{{ expense.notes '
                      '}}</textarea></label>\n'
                      '</div>\n'
                      '<div class="callout info">Wartość w CZK: <strong id="expense-czk-preview">0,00 '
                      'CZK</strong></div>\n'
                      '<div class="form-actions"><a class="btn btn-ghost" href="{{ url_for(\'expenses_page\') '
                      '}}">Anuluj</a><button class="btn btn-primary" type="submit">Zapisz koszt</button></div>\n'
                      '</form>\n'
                      '{% if is_edit %}<div class="danger-zone"><span></span><form method="post" action="{{ '
                      'url_for(\'expense_delete\', expense_id=expense.id) }}" onsubmit="return confirm(\'Usunąć ten '
                      'koszt?\')"><button class="btn btn-danger" type="submit">Usuń koszt</button></form></div>{% '
                      'endif %}\n'
                      '{% endblock %}\n',
 'expenses.html': '{% extends "base.html" %}\n'
                  '{% block title %}Koszty - Faktury OSVČ{% endblock %}\n'
                  '{% block content %}\n'
                  '<div class="page-header"><div><h1>Koszty działalności</h1><p class="muted">Bieżąca ewidencja '
                  'wydatków i prosty wynik miesięczny.</p></div><div class="button-row"><a class="btn btn-light" '
                  'href="{{ url_for(\'expenses_export_csv\', year=selected_year, month=selected_month) }}">Eksport '
                  'CSV</a><a class="btn btn-primary" href="{{ url_for(\'expense_new\') }}">+ Dodaj '
                  'koszt</a></div></div>\n'
                  '<form class="toolbar" method="get"><select name="month">{% for value, label in MONTHS.items() '
                  '%}<option value="{{ value }}" {% if selected_month == value %}selected{% endif %}>{{ label '
                  '}}</option>{% endfor %}</select><input type="number" name="year" value="{{ selected_year }}" '
                  'min="2020" max="2100"><button class="btn btn-light" type="submit">Pokaż okres</button></form>\n'
                  '<div class="stats-grid dashboard-stats"><div class="stat-card"><span class="stat-value '
                  'compact-value">{{ totals.revenue|money(\'CZK\') }}</span><span class="stat-label">Przychód z '
                  'faktur</span></div><div class="stat-card"><span class="stat-value compact-value">{{ '
                  'totals.actual|money(\'CZK\') }}</span><span class="stat-label">Wydatki rzeczywiste</span></div><div '
                  'class="stat-card"><span class="stat-value compact-value">{{ totals.deductible|money(\'CZK\') '
                  '}}</span><span class="stat-label">Część kosztowa</span></div><div class="stat-card"><span '
                  'class="stat-value compact-value">{{ totals.result|money(\'CZK\') }}</span><span '
                  'class="stat-label">Wynik przed podatkami</span></div></div>\n'
                  '{% if missing_rates %}<div class="callout warning">Nie wszystkie faktury lub koszty mają kurs do '
                  'CZK. Suma może być niepełna.</div>{% endif %}\n'
                  '{% if foreign_dph_count %}<div class="callout warning"><strong>DPH:</strong> {{ foreign_dph_count '
                  '}} kosztów oznaczono jako zagraniczne usługi lub do sprawdzenia. Zobacz zakładkę <a href="{{ '
                  'url_for(\'dph_dashboard\', year=selected_year, month=selected_month) }}">DPH / UE</a>.</div>{% '
                  'endif %}\n'
                  '{% if category_totals %}<section class="panel"><div class="panel-header"><h2>Podział kosztów - {{ '
                  'period_label }}</h2></div><div class="category-grid">{% for item in category_totals %}<div '
                  'class="category-card"><span>{{ EXPENSE_CATEGORIES[item.category] }}</span><strong>{{ '
                  "item.total|money('CZK') }}</strong></div>{% endfor %}</div></section>{% endif %}\n"
                  '<section class="panel"><div class="panel-header"><h2>Wydatki - {{ period_label }}</h2></div>{% if '
                  'expenses %}<div '
                  'class="table-wrap"><table><thead><tr><th>Data</th><th>Opis</th><th>Dostawca</th><th>Kategoria</th><th>Dokument</th><th>Kwota</th><th>W '
                  'CZK</th><th></th></tr></thead><tbody>{% for expense in expenses %}<tr><td>{{ '
                  'expense.expense_date|date_pl }}</td><td><strong>{{ expense.description }}</strong>{% if '
                  'expense.dph_obligation != \'none\' %}<br><span class="badge badge-warning">DPH do '
                  "sprawdzenia</span>{% endif %}</td><td>{{ expense.supplier or '—' }}</td><td>{{ "
                  "EXPENSE_CATEGORIES[expense.category] }}</td><td>{{ expense.document_number or '—' }}</td><td>{{ "
                  "expense.amount|money(expense.currency) }}</td><td>{{ expense.amount_czk|money('CZK') if "
                  'expense.amount_czk is not none else \'brak kursu\' }}</td><td><a class="btn btn-light btn-small" '
                  'href="{{ url_for(\'expense_edit\', expense_id=expense.id) }}">Edytuj</a></td></tr>{% endfor '
                  '%}</tbody></table></div>{% else %}<div class="empty-state"><h3>Brak kosztów w tym miesiącu</h3><a '
                  'class="btn btn-primary" href="{{ url_for(\'expense_new\') }}">Dodaj pierwszy koszt</a></div>{% '
                  'endif %}</section>\n'
                  '{% endblock %}\n',
 'invoice_detail.html': '{% extends "base.html" %}\n'
                        '{% block title %}{{ invoice.invoice_number }} - Faktury OSVČ{% endblock %}\n'
                        '{% block content %}\n'
                        '<div class="page-header"><div><h1>{{ invoice.invoice_number }}</h1><p class="muted">{{ '
                        'invoice.contractor_name }} · {{ invoice.issue_date|date_pl }}</p></div><div '
                        'class="button-row"><a class="btn btn-primary" href="{{ url_for(\'invoice_pdf\', '
                        'invoice_id=invoice.id) }}">Pobierz PDF A4</a><a class="btn btn-light" href="{{ '
                        'url_for(\'invoice_edit\', invoice_id=invoice.id) }}">Edytuj</a></div></div>\n'
                        '\n'
                        '{% if dph_info.kind == \'eu_service\' %}<div class="callout info"><strong>DPH / UE:</strong> '
                        'ta faktura zostanie ujęta w Souhrnné hlášení za {{ dph_info.period_label }} z kodem plnění 3. '
                        'Podstawowy termin: {{ dph_info.due_date|date_pl }}. <a href="{{ url_for(\'dph_dashboard\', '
                        'year=invoice.supply_date[:4], month=invoice.supply_date[5:7]) }}">Otwórz raport</a>.</div>{% '
                        'elif dph_info.warning %}<div class="callout warning"><strong>DPH do sprawdzenia:</strong> {{ '
                        'dph_info.warning }}</div>{% endif %}\n'
                        '\n'
                        '<div class="detail-grid">\n'
                        '<section class="panel"><div class="panel-header"><h2>Dane faktury</h2><span class="badge '
                        'badge-{{ invoice.status }}">{{ \'Opłacona\' if invoice.status == \'paid\' else '
                        '\'Nieopłacona\' }}</span></div><dl class="details">\n'
                        '<div><dt>Kontrahent</dt><dd>{{ invoice.contractor_name }}</dd></div><div><dt>VAT '
                        "ID</dt><dd>{{ invoice.contractor_vat_id or '—' }}</dd></div><div><dt>Data "
                        'wystawienia</dt><dd>{{ invoice.issue_date|date_pl }}</dd></div><div><dt>Data '
                        'usługi</dt><dd>{{ invoice.supply_date|date_pl }}</dd></div><div><dt>Termin '
                        'płatności</dt><dd>{{ invoice.due_date|date_pl }}</dd></div><div><dt>Tryb VAT</dt><dd>{{ '
                        'TAX_MODES[invoice.tax_mode] }}</dd></div><div><dt>Kurs do CZK</dt><dd>{{ invoice.czk_rate or '
                        "('1' if invoice.currency == 'CZK' else '—') }}</dd></div><div><dt>DPH / VIES</dt><dd>{{ "
                        'dph_info.label }}</dd></div></dl></section>\n'
                        '<section class="panel total-card"><span class="muted">Do zapłaty</span><strong>{{ '
                        'totals.total_gross|money(invoice.currency) }}</strong>{% if dph_info.value_czk is not none '
                        "%}<small>Do ewidencji: {{ dph_info.value_czk|money('CZK') }}</small>{% endif %}</section>\n"
                        '</div>\n'
                        '<section class="panel"><h2>Pozycje</h2><div '
                        'class="table-wrap"><table><thead><tr><th>Opis</th><th>Ilość</th><th>Jedn.</th><th>Cena</th>{% '
                        "if invoice.tax_mode == 'vat' %}<th>VAT</th>{% endif %}<th>Wartość</th></tr></thead><tbody>{% "
                        'for item in items %}<tr><td>{{ item.description }}</td><td>{{ item.quantity_decimal '
                        '}}</td><td>{{ item.unit }}</td><td>{{ item.unit_price_decimal|money(invoice.currency) '
                        "}}</td>{% if invoice.tax_mode == 'vat' %}<td>{{ item.vat_rate_decimal }}%</td>{% endif "
                        '%}<td>{{ item.gross|money(invoice.currency) }}</td></tr>{% endfor '
                        '%}</tbody></table></div></section>\n'
                        '{% if invoice.reverse_charge_note or invoice.notes %}<section class="panel">{% if '
                        'invoice.reverse_charge_note %}<h2>Adnotacja</h2><p>{{ invoice.reverse_charge_note }}</p>{% '
                        'endif %}{% if invoice.notes %}<h2>Uwagi</h2><p class="preline">{{ invoice.notes }}</p>{% '
                        'endif %}</section>{% endif %}\n'
                        '<div class="danger-zone"><form method="post" action="{{ url_for(\'invoice_toggle_paid\', '
                        'invoice_id=invoice.id) }}"><button class="btn btn-light" type="submit">{{ \'Oznacz jako '
                        "nieopłaconą' if invoice.status == 'paid' else 'Oznacz jako opłaconą' }}</button></form><form "
                        'method="post" action="{{ url_for(\'invoice_delete\', invoice_id=invoice.id) }}" '
                        'onsubmit="return confirm(\'Usunąć fakturę {{ invoice.invoice_number }}?\')"><button '
                        'class="btn btn-danger" type="submit">Usuń fakturę</button></form></div>\n'
                        '{% endblock %}\n',
 'invoice_form.html': '{% extends "base.html" %}\n'
                      "{% block title %}{{ 'Edytuj fakturę' if is_edit else 'Nowa faktura' }} - Faktury OSVČ{% "
                      'endblock %}\n'
                      '{% block content %}\n'
                      '<div class="page-header">\n'
                      "    <div><h1>{{ 'Edytuj fakturę ' ~ invoice.invoice_number if is_edit else 'Nowa faktura' "
                      '}}</h1><p class="muted">PDF zostanie wygenerowany w formacie A4.</p></div>\n'
                      '</div>\n'
                      '{% if not contractors %}<div class="callout warning"><strong>Najpierw dodaj '
                      'kontrahenta.</strong> <a href="{{ url_for(\'contractor_new\') }}">Dodaj teraz</a>.</div>{% '
                      'endif %}\n'
                      '<form method="post" class="panel form-panel invoice-form" data-default-due-days="{{ '
                      'company.default_due_days }}">\n'
                      '    <h2>Dane dokumentu</h2>\n'
                      '    <div class="form-grid three">\n'
                      '        <label class="field span-2"><span>Kontrahent *</span><select id="contractor-select" '
                      'name="contractor_id" required><option value="">-- wybierz --</option>{% for contractor in '
                      'contractors %}<option value="{{ contractor.id }}" data-vat="{{ contractor.vat_id }}" '
                      'data-country="{{ contractor.country }}" {% if invoice.contractor_id|string == '
                      'contractor.id|string %}selected{% endif %}>{{ contractor.name }}{% if contractor.vat_id %} ({{ '
                      'contractor.vat_id }}){% endif %}</option>{% endfor %}</select></label>\n'
                      '        <div class="field field-button"><span>&nbsp;</span><a class="btn btn-light" href="{{ '
                      'url_for(\'contractor_new\') }}">+ Nowy kontrahent</a></div>\n'
                      '        <label class="field"><span>Data wystawienia *</span><input id="issue-date" type="date" '
                      'name="issue_date" value="{{ invoice.issue_date }}" required></label>\n'
                      '        <label class="field"><span>Data wykonania usługi *</span><input type="date" '
                      'name="supply_date" value="{{ invoice.supply_date }}" required></label>\n'
                      '        <label class="field"><span>Termin płatności *</span><input id="due-date" type="date" '
                      'name="due_date" value="{{ invoice.due_date }}" required></label>\n'
                      '        <label class="field"><span>Waluta</span><input id="invoice-currency" name="currency" '
                      'value="{{ invoice.currency }}" maxlength="6" list="currency-list"><datalist '
                      'id="currency-list"><option value="EUR"><option value="CZK"><option value="PLN"><option '
                      'value="USD"></datalist></label>\n'
                      '        <label class="field"><span>Kurs do CZK</span><input id="czk-rate" type="number" '
                      'step="0.0001" min="0" name="czk_rate" value="{{ invoice.czk_rate }}" placeholder="np. '
                      '24,85"><small>1 jednostka waluty faktury = ... CZK</small></label>\n'
                      '        <label class="field"><span>Język PDF</span><select name="language">{% for code, label '
                      'in LANGUAGES.items() %}<option value="{{ code }}" {% if invoice.language == code %}selected{% '
                      'endif %}>{{ label }}</option>{% endfor %}</select></label>\n'
                      '        <label class="field"><span>Tryb VAT</span><select id="tax-mode" name="tax_mode">{% for '
                      'code, label in TAX_MODES.items() %}<option value="{{ code }}" {% if invoice.tax_mode == code '
                      '%}selected{% endif %}>{{ label }}</option>{% endfor %}</select></label>\n'
                      '        <label class="field span-2"><span>Klasyfikacja DPH / VIES</span><select '
                      'id="dph-category" name="dph_category">{% for code, label in DPH_CATEGORIES.items() %}<option '
                      'value="{{ code }}" {% if invoice.dph_category == code %}selected{% endif %}>{{ label '
                      '}}</option>{% endfor %}</select></label>\n'
                      '        <label class="field"><span>Sposób płatności</span><select name="payment_method">{% for '
                      'code, label in PAYMENT_METHODS.items() %}<option value="{{ code }}" {% if '
                      'invoice.payment_method == code %}selected{% endif %}>{{ label }}</option>{% endfor '
                      '%}</select></label>\n'
                      '        <label class="field"><span>Numer zamówienia</span><input name="order_number" value="{{ '
                      'invoice.order_number }}"></label>\n'
                      '        <label class="field"><span>Symbol / referencja płatności</span><input '
                      'name="variable_symbol" value="{{ invoice.variable_symbol }}" placeholder="Wygeneruje się '
                      'automatycznie"></label>\n'
                      '    </div>\n'
                      '    <div id="dph-hint" class="callout info dph-hint">Program sprawdzi VAT ID kontrahenta i tryb '
                      'faktury.</div>\n'
                      '    <p class="small muted">Automatyczna klasyfikacja jest podpowiedzią. Dla usług związanych z '
                      'nieruchomością lub budową, zaliczek, korekt oraz nietypowych transakcji wybierz „Do ręcznego '
                      'sprawdzenia”.</p>\n'
                      '\n'
                      '    <hr>\n'
                      '    <div class="panel-header"><h2>Pozycje faktury</h2><button id="add-item" class="btn '
                      'btn-light btn-small" type="button">+ Dodaj pozycję</button></div>\n'
                      '    <div class="table-wrap invoice-items-wrap">\n'
                      '        <table class="invoice-items">\n'
                      '            <thead><tr><th>Opis</th><th>Ilość</th><th>Jedn.</th><th>Cena jedn.</th><th '
                      'class="vat-column">VAT %</th><th>Wartość</th><th></th></tr></thead>\n'
                      '            <tbody id="invoice-items-body">\n'
                      '                {% for item in items %}\n'
                      '                <tr class="invoice-item-row">\n'
                      '                    <td><input name="item_description" value="{{ item.description }}" '
                      'placeholder="Opis usługi" required></td>\n'
                      '                    <td><input class="qty-input" type="number" step="0.01" min="0.01" '
                      'name="item_quantity" value="{{ item.quantity }}" required></td>\n'
                      '                    <td><input name="item_unit" value="{{ item.unit }}" placeholder="h"></td>\n'
                      '                    <td><input class="price-input" type="number" step="0.01" min="0" '
                      'name="item_unit_price" value="{{ item.unit_price }}" required></td>\n'
                      '                    <td class="vat-column"><input class="vat-input" type="number" step="0.01" '
                      'min="0" max="100" name="item_vat_rate" value="{{ item.vat_rate or 0 }}"></td>\n'
                      '                    <td class="line-total">0.00</td>\n'
                      '                    <td><button class="icon-button remove-item" type="button" title="Usuń '
                      'pozycję">×</button></td>\n'
                      '                </tr>\n'
                      '                {% endfor %}\n'
                      '            </tbody>\n'
                      '            <tfoot><tr><td colspan="5" class="total-label">Razem</td><td '
                      'id="invoice-total">0.00 {{ invoice.currency }}</td><td></td></tr></tfoot>\n'
                      '        </table>\n'
                      '    </div>\n'
                      '\n'
                      '    <div id="reverse-charge-section" class="form-grid one"><label class="field"><span>Adnotacja '
                      'reverse charge</span><textarea name="reverse_charge_note" rows="3">{{ '
                      'invoice.reverse_charge_note }}</textarea></label></div>\n'
                      '    <label class="field"><span>Uwagi na fakturze</span><textarea name="notes" rows="4">{{ '
                      'invoice.notes }}</textarea></label>\n'
                      '    <div class="form-actions"><a class="btn btn-ghost" href="{{ url_for(\'invoices_list\') '
                      '}}">Anuluj</a><button class="btn btn-primary" type="submit" {% if not contractors %}disabled{% '
                      "endif %}>{{ 'Zapisz zmiany' if is_edit else 'Wystaw fakturę' }}</button></div>\n"
                      '</form>\n'
                      '\n'
                      '<template id="invoice-item-template"><tr class="invoice-item-row"><td><input '
                      'name="item_description" placeholder="Opis usługi" required></td><td><input class="qty-input" '
                      'type="number" step="0.01" min="0.01" name="item_quantity" value="1" required></td><td><input '
                      'name="item_unit" value="h"></td><td><input class="price-input" type="number" step="0.01" '
                      'min="0" name="item_unit_price" required></td><td class="vat-column"><input class="vat-input" '
                      'type="number" step="0.01" min="0" max="100" name="item_vat_rate" value="0"></td><td '
                      'class="line-total">0.00</td><td><button class="icon-button remove-item" '
                      'type="button">×</button></td></tr></template>\n'
                      '{% endblock %}\n',
 'invoices_list.html': '{% extends "base.html" %}\n'
                       '{% block title %}Faktury - Faktury OSVČ{% endblock %}\n'
                       '{% block content %}\n'
                       '<div class="page-header">\n'
                       '    <div><h1>Faktury</h1><p class="muted">Lista wystawionych dokumentów.</p></div>\n'
                       '    <a class="btn btn-primary" href="{{ url_for(\'invoice_new\') }}">+ Nowa faktura</a>\n'
                       '</div>\n'
                       '<form class="toolbar" method="get">\n'
                       '    <input type="search" name="q" value="{{ q }}" placeholder="Numer, kontrahent lub VAT ID">\n'
                       '    <select name="status"><option value="">Wszystkie statusy</option><option value="unpaid" {% '
                       'if status == \'unpaid\' %}selected{% endif %}>Nieopłacone</option><option value="paid" {% if '
                       "status == 'paid' %}selected{% endif %}>Opłacone</option></select>\n"
                       '    <button class="btn btn-light" type="submit">Filtruj</button>\n'
                       '    {% if q or status %}<a class="btn btn-ghost" href="{{ url_for(\'invoices_list\') '
                       '}}">Wyczyść</a>{% endif %}\n'
                       '</form>\n'
                       '<section class="panel">\n'
                       '{% if invoices %}\n'
                       '<div class="table-wrap"><table>\n'
                       '<thead><tr><th>Numer</th><th>Kontrahent</th><th>Wystawiona</th><th>Termin</th><th>Kwota</th><th>Status</th><th></th></tr></thead>\n'
                       '<tbody>\n'
                       '{% for invoice in invoices %}\n'
                       '<tr>\n'
                       '    <td><a href="{{ url_for(\'invoice_view\', invoice_id=invoice.id) }}"><strong>{{ '
                       'invoice.invoice_number }}</strong></a></td>\n'
                       '    <td>{{ invoice.contractor_name }}</td>\n'
                       '    <td>{{ invoice.issue_date|date_pl }}</td>\n'
                       '    <td>{{ invoice.due_date|date_pl }}</td>\n'
                       '    <td>{{ invoice.total|money(invoice.currency) }}</td>\n'
                       '    <td><span class="badge badge-{{ invoice.status }}">{{ \'Opłacona\' if invoice.status == '
                       "'paid' else 'Nieopłacona' }}</span></td>\n"
                       '    <td class="actions">\n'
                       '        <a class="btn btn-light btn-small" href="{{ url_for(\'invoice_pdf\', '
                       'invoice_id=invoice.id) }}">PDF</a>\n'
                       '        <a class="btn btn-light btn-small" href="{{ url_for(\'invoice_edit\', '
                       'invoice_id=invoice.id) }}">Edytuj</a>\n'
                       '    </td>\n'
                       '</tr>\n'
                       '{% endfor %}\n'
                       '</tbody></table></div>\n'
                       '{% else %}\n'
                       '<div class="empty-state"><h3>Brak faktur</h3><p>Wystaw pierwszą fakturę w formacie A4 '
                       'PDF.</p><a class="btn btn-primary" href="{{ url_for(\'invoice_new\') }}">Nowa '
                       'faktura</a></div>\n'
                       '{% endif %}\n'
                       '</section>\n'
                       '{% endblock %}\n',
 'tools.html': '{% extends "base.html" %}\n'
               '{% block title %}Narzędzia - Faktury OSVČ{% endblock %}\n'
               '{% block content %}\n'
               '<div class="page-header"><div><h1>Narzędzia i kopie zapasowe</h1><p class="muted">Reset faktur nie '
               'usuwa kontrahentów, kosztów ani danych firmy.</p></div></div>\n'
               '<div class="stats-grid"><div class="stat-card"><span class="stat-value">{{ counts.invoices '
               '}}</span><span class="stat-label">Faktury</span></div><div class="stat-card"><span '
               'class="stat-value">{{ counts.contractors }}</span><span '
               'class="stat-label">Kontrahenci</span></div><div class="stat-card"><span class="stat-value">{{ '
               'counts.expenses }}</span><span class="stat-label">Koszty</span></div><div class="stat-card"><span '
               'class="stat-value">{{ next_numbers }}</span><span class="stat-label">Kolejne '
               'numery</span></div></div>\n'
               '<section class="panel"><div class="panel-header"><h2>Folder faktur PDF</h2></div><p class="muted '
               'preline">{{ invoices_path }}</p><div class="button-row"><a class="btn btn-primary" href="{{ '
               'url_for(\'open_invoices_folder\') }}">Otwórz folder faktur</a></div></section>\n'
               '<section class="panel"><div class="panel-header"><h2>Kopia bazy danych</h2></div><p>Plik zawiera dane '
               'firmy, kontrahentów, faktury, koszty i statusy zgłoszeń DPH.</p><div class="button-row"><a class="btn '
               'btn-primary" href="{{ url_for(\'backup_database\') }}">Pobierz kopię bazy</a></div></section>\n'
               '<section class="panel"><div class="panel-header"><h2>Import starej bazy</h2></div><p>Wybierz stary '
               'plik <code>data/invoice_app.db</code> albo kopię <code>.sqlite3</code>. Program automatycznie doda '
               'nowe tabele i pola bez usuwania kontrahentów.</p><form method="post" action="{{ '
               'url_for(\'import_database\') }}" enctype="multipart/form-data" class="form-panel compact-form"><label '
               'class="field"><span>Plik starej bazy</span><input type="file" name="database" '
               'accept=".db,.sqlite,.sqlite3" required></label><div class="form-actions"><button class="btn btn-light" '
               'type="submit" onclick="return confirm(\'Zaimportować wybraną bazę?\')">Importuj '
               'bazę</button></div></form></section>\n'
               '<section class="panel danger-panel"><div class="panel-header"><h2>Usuń faktury próbne i zacznij '
               'numerację od 1</h2></div><div class="callout warning"><strong>Tylko dla dokumentów testowych.</strong> '
               'Faktury i statusy SH zostaną usunięte. Kontrahenci, ustawienia podatków, składek i dane firmy pozostaną.</div><form '
               'method="post" action="{{ url_for(\'reset_invoices\') }}" class="form-panel compact-form" '
               'onsubmit="return confirm(\'Usunąć wszystkie faktury próbne?\')"><label class="field"><span>Wpisz '
               'RESET</span><input name="confirmation" placeholder="RESET" required></label><div '
               'class="form-actions"><button class="btn btn-danger" type="submit">Usuń faktury i zresetuj '
               'numerację</button></div></form></section>\n'
               '<section class="panel"><h2>Chmura / trwałość danych</h2>'
               '<p>{% if remote_db_enabled %}<strong>Aktywna:</strong> baza jest synchronizowana z prywatnym Supabase Storage po każdej zmianie.'
               '<br><span class="muted">Bucket: {{ remote_bucket }}</span>{% else %}<strong>Nieaktywna.</strong> W wersji lokalnej dane są tylko na tym komputerze.{% endif %}</p>'
               '</section>'
               '<section class="panel"><h2>Gdzie są dane?</h2><p class="muted preline">{{ db_path }}</p><p '
               'class="small muted">Sam program jest jednym plikiem, natomiast baza musi pozostać osobno, aby dane '
               'przetrwały aktualizacje.</p></section>\n'
               '{% endblock %}\n'}

EMBEDDED_CSS = ':root {\n    --bg: #f5f6f8;\n    --panel: #ffffff;\n    --text: #20252b;\n    --muted: #6d737c;\n    --line: #dfe3e8;\n    --primary: #7a1735;\n    --primary-dark: #5b1027;\n    --primary-soft: #f4eaf0;\n    --danger: #b42318;\n    --success: #147a43;\n    --warning: #a15c00;\n    --shadow: 0 8px 24px rgba(25, 31, 40, .07);\n}\n* { box-sizing: border-box; }\nhtml { min-height: 100%; }\nbody { margin: 0; min-height: 100vh; display: flex; flex-direction: column; font-family: Inter, Segoe UI, Arial, sans-serif; color: var(--text); background: var(--bg); }\na { color: var(--primary); text-decoration: none; }\na:hover { text-decoration: underline; }\n.topbar { position: sticky; top: 0; z-index: 20; background: #fff; border-bottom: 1px solid var(--line); }\n.topbar-inner { max-width: 1180px; margin: 0 auto; min-height: 68px; padding: 0 24px; display: flex; align-items: center; gap: 26px; }\n.brand { font-size: 21px; font-weight: 800; color: var(--primary); letter-spacing: -.4px; white-space: nowrap; }\n.brand:hover { text-decoration: none; }\n.nav { display: flex; gap: 8px; flex: 1; }\n.nav a { padding: 10px 12px; color: #39404a; border-radius: 8px; }\n.nav a:hover { background: var(--primary-soft); text-decoration: none; }\n.container { width: min(1180px, calc(100% - 32px)); margin: 0 auto; }\nmain.container { flex: 1; padding-top: 34px; padding-bottom: 52px; }\n.footer { border-top: 1px solid var(--line); background: #fff; color: var(--muted); font-size: 13px; }\n.footer-inner { padding-top: 18px; padding-bottom: 18px; display: flex; justify-content: space-between; gap: 16px; }\nh1, h2, h3 { margin-top: 0; line-height: 1.2; }\nh1 { margin-bottom: 8px; font-size: 30px; letter-spacing: -.6px; }\nh2 { margin-bottom: 18px; font-size: 19px; }\np { line-height: 1.55; }\n.muted { color: var(--muted); }\n.small { font-size: 12px; }\n.preline { white-space: pre-line; }\n.page-header { display: flex; align-items: center; justify-content: space-between; gap: 18px; margin-bottom: 24px; }\n.button-row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }\n.button-row.center { justify-content: center; }\n.btn { display: inline-flex; align-items: center; justify-content: center; min-height: 42px; padding: 10px 17px; border-radius: 8px; border: 1px solid transparent; font: inherit; font-weight: 700; cursor: pointer; text-decoration: none; transition: .15s ease; }\n.btn:hover { text-decoration: none; transform: translateY(-1px); }\n.btn:disabled { opacity: .5; cursor: not-allowed; transform: none; }\n.btn-primary { color: #fff; background: var(--primary); }\n.btn-primary:hover { background: var(--primary-dark); }\n.btn-light { color: #2f3640; background: #fff; border-color: #cfd4da; }\n.btn-light:hover { background: #f6f7f8; }\n.btn-ghost { color: #4c535d; background: transparent; }\n.btn-danger { color: #fff; background: var(--danger); }\n.btn-small { min-height: 34px; padding: 7px 11px; font-size: 13px; }\n.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; box-shadow: var(--shadow); padding: 24px; margin-bottom: 24px; }\n.panel-header { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 16px; }\n.panel-header h2 { margin-bottom: 0; }\n.stats-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 18px; margin-bottom: 24px; }\n.stat-card { background: #fff; border: 1px solid var(--line); border-radius: 12px; box-shadow: var(--shadow); padding: 24px; display: flex; flex-direction: column; gap: 5px; }\n.stat-value { font-size: 32px; font-weight: 800; color: var(--primary); }\n.stat-label { color: var(--muted); }\n.table-wrap { width: 100%; overflow-x: auto; }\ntable { width: 100%; border-collapse: collapse; }\nth, td { padding: 13px 12px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: middle; }\nth { color: #555d67; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; background: #fafbfc; }\ntbody tr:hover { background: #fcfcfd; }\n.actions { text-align: right; white-space: nowrap; }\n.inline-form { display: inline; }\n.badge { display: inline-block; padding: 5px 9px; border-radius: 999px; font-size: 12px; font-weight: 800; }\n.badge-paid { background: #e9f7ef; color: var(--success); }\n.badge-unpaid { background: #fff1e6; color: var(--warning); }\n.empty-state { text-align: center; padding: 48px 24px; color: var(--muted); }\n.empty-state h3, .empty-state h2, .empty-state h1 { color: var(--text); }\n.page-empty { padding-top: 100px; }\n.toolbar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 18px; }\n.toolbar input, .toolbar select { min-width: 240px; }\ninput, select, textarea { width: 100%; border: 1px solid #cbd1d8; border-radius: 8px; background: #fff; color: var(--text); font: inherit; padding: 10px 12px; outline: none; }\ninput:focus, select:focus, textarea:focus { border-color: var(--primary); box-shadow: 0 0 0 3px rgba(122, 23, 53, .11); }\ntextarea { resize: vertical; }\n.form-panel hr { border: 0; border-top: 1px solid var(--line); margin: 28px 0; }\n.form-grid { display: grid; gap: 18px; }\n.form-grid.one { grid-template-columns: 1fr; }\n.form-grid.two { grid-template-columns: repeat(2, 1fr); }\n.form-grid.three { grid-template-columns: repeat(3, 1fr); }\n.field { display: flex; flex-direction: column; gap: 7px; }\n.field > span { font-size: 13px; font-weight: 700; color: #4b535d; }\n.field-button { justify-content: flex-end; }\n.span-2 { grid-column: span 2; }\n.span-3 { grid-column: span 3; }\n.form-actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 28px; padding-top: 22px; border-top: 1px solid var(--line); }\n.callout { padding: 15px 17px; border-radius: 9px; margin-bottom: 20px; border: 1px solid; }\n.callout.warning { background: #fff8e7; border-color: #ebcf8b; color: #664400; }\n.flash-stack { margin-bottom: 20px; display: grid; gap: 10px; }\n.flash { padding: 14px 16px; border-radius: 8px; border: 1px solid; }\n.flash-success { background: #ecf8f1; color: #116c3a; border-color: #b6dfc8; }\n.flash-error { background: #fff0ef; color: #9d1c15; border-color: #efbab6; }\n.invoice-items-wrap { margin-bottom: 20px; }\n.invoice-items { min-width: 920px; }\n.invoice-items input { min-width: 80px; padding: 8px 9px; }\n.invoice-items td:first-child input { min-width: 300px; }\n.invoice-items tfoot td { font-weight: 800; background: var(--primary-soft); }\n.total-label { text-align: right; }\n.line-total { white-space: nowrap; font-weight: 700; }\n.icon-button { width: 32px; height: 32px; border: 1px solid #d8dde3; border-radius: 7px; background: #fff; color: var(--danger); font-size: 20px; cursor: pointer; }\n.detail-grid { display: grid; grid-template-columns: 2fr 1fr; gap: 24px; }\n.details { display: grid; grid-template-columns: repeat(2, 1fr); gap: 18px 26px; margin: 0; }\n.details div { border-bottom: 1px solid var(--line); padding-bottom: 10px; }\n.details dt { color: var(--muted); font-size: 12px; margin-bottom: 4px; }\n.details dd { margin: 0; font-weight: 700; }\n.total-card { display: flex; flex-direction: column; justify-content: center; text-align: center; gap: 10px; min-height: 200px; }\n.total-card strong { font-size: 30px; color: var(--primary); }\n.total-card small { color: var(--muted); }\n.danger-zone { display: flex; justify-content: space-between; align-items: center; gap: 15px; margin-top: 30px; }\n@media (max-width: 850px) {\n    .topbar-inner { flex-wrap: wrap; padding-top: 12px; padding-bottom: 12px; gap: 10px; }\n    .nav { order: 3; width: 100%; overflow-x: auto; }\n    .page-header { align-items: flex-start; flex-direction: column; }\n    .stats-grid, .detail-grid { grid-template-columns: 1fr; }\n    .form-grid.two, .form-grid.three { grid-template-columns: 1fr; }\n    .span-2, .span-3 { grid-column: span 1; }\n    .details { grid-template-columns: 1fr; }\n}\n@media (max-width: 560px) {\n    .container { width: min(100% - 20px, 1180px); }\n    .panel { padding: 17px; }\n    .footer-inner { flex-direction: column; }\n    .danger-zone { align-items: stretch; flex-direction: column; }\n    .danger-zone form, .danger-zone button { width: 100%; }\n}\n\n\n.compact-form { max-width: 760px; padding: 0; box-shadow: none; border: 0; }\n.danger-panel { border: 1px solid #f3b4b4; }\n.danger-panel h2 { color: #9d1c1c; }\ncode { background: #f2f4f7; padding: 2px 6px; border-radius: 5px; }\n\n\n/* Moduły kosztów i DPH */\n.dashboard-stats { grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); }\n.compact-value { font-size: 22px; word-break: break-word; }\n.status-value { font-size: 21px; }\n.callout.info { background: #edf6ff; border-color: #b8d6ef; color: #244b68; }\n.badge-warning { background: #fff1e6; color: var(--warning); }\n.category-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }\n.category-card { border: 1px solid var(--line); border-radius: 9px; padding: 14px; display: flex; flex-direction: column; gap: 7px; }\n.category-card span { color: var(--muted); font-size: 13px; }\n.category-card strong { font-size: 18px; }\n.compact-form { max-width: 760px; padding: 0; box-shadow: none; border: 0; }\n.danger-panel { border: 1px solid #f3b4b4; }\n.danger-panel h2 { color: #9d1c1c; }\n.compact-list { margin: 8px 0 0; padding-left: 20px; }\n.disabled-link { opacity: .45; pointer-events: none; }\n.field small { color: var(--muted); font-size: 11px; line-height: 1.35; }\n.dph-hint { margin-top: 18px; margin-bottom: 0; }\ncode { background: #f2f4f7; padding: 2px 6px; border-radius: 5px; }\n'

EMBEDDED_JS = '(function () {\n    function parseNumber(value) {\n        const normalized = String(value || \'\').replace(/\\s/g, \'\').replace(\',\', \'.\');\n        const parsed = Number.parseFloat(normalized);\n        return Number.isFinite(parsed) ? parsed : 0;\n    }\n    function currency() {\n        const input = document.querySelector(\'input[name="currency"]\');\n        return input ? input.value.trim().toUpperCase() : \'\';\n    }\n    function recalculate() {\n        const mode = document.getElementById(\'tax-mode\');\n        const vatEnabled = mode && mode.value === \'vat\';\n        let total = 0;\n        document.querySelectorAll(\'.invoice-item-row\').forEach((row) => {\n            const qty = parseNumber(row.querySelector(\'.qty-input\')?.value);\n            const price = parseNumber(row.querySelector(\'.price-input\')?.value);\n            const vatRate = vatEnabled ? parseNumber(row.querySelector(\'.vat-input\')?.value) : 0;\n            const gross = qty * price * (1 + vatRate / 100);\n            total += gross;\n            const cell = row.querySelector(\'.line-total\');\n            if (cell) cell.textContent = `${gross.toFixed(2)} ${currency()}`;\n        });\n        const totalCell = document.getElementById(\'invoice-total\');\n        if (totalCell) totalCell.textContent = `${total.toFixed(2)} ${currency()}`;\n    }\n    function updateTaxMode() {\n        const mode = document.getElementById(\'tax-mode\');\n        if (!mode) return;\n        const vatEnabled = mode.value === \'vat\';\n        document.querySelectorAll(\'.vat-column\').forEach((el) => { el.style.display = vatEnabled ? \'\' : \'none\'; });\n        document.querySelectorAll(\'.vat-input\').forEach((input) => { input.disabled = !vatEnabled; if (!vatEnabled) input.value = \'0\'; });\n        const reverse = document.getElementById(\'reverse-charge-section\');\n        if (reverse) reverse.style.display = mode.value === \'reverse_charge\' ? \'\' : \'none\';\n        updateDphHint();\n        recalculate();\n    }\n    function addItem() {\n        const template = document.getElementById(\'invoice-item-template\');\n        const body = document.getElementById(\'invoice-items-body\');\n        if (!template || !body) return;\n        body.appendChild(template.content.cloneNode(true));\n        updateTaxMode();\n        body.querySelector(\'tr:last-child input[name="item_description"]\')?.focus();\n    }\n    function updateDphHint() {\n        const hint = document.getElementById(\'dph-hint\');\n        const contractor = document.getElementById(\'contractor-select\');\n        const taxMode = document.getElementById(\'tax-mode\');\n        const category = document.getElementById(\'dph-category\');\n        if (!hint || !contractor || !taxMode || !category) return;\n        const option = contractor.options[contractor.selectedIndex];\n        const vat = String(option?.dataset?.vat || \'\').replace(/[^A-Za-z0-9]/g, \'\').toUpperCase();\n        const prefix = vat.slice(0, 2);\n        const eu = [\'AT\',\'BE\',\'BG\',\'CY\',\'CZ\',\'DE\',\'DK\',\'EE\',\'EL\',\'ES\',\'FI\',\'FR\',\'HR\',\'HU\',\'IE\',\'IT\',\'LT\',\'LU\',\'LV\',\'MT\',\'NL\',\'PL\',\'PT\',\'RO\',\'SE\',\'SI\',\'SK\'];\n        if (category.value === \'eu_service\') {\n            hint.innerHTML = \'<strong>Ręcznie wybrano:</strong> usługa B2B UE - Souhrnné hlášení, kod 3.\';\n        } else if (category.value === \'not_required\') {\n            hint.innerHTML = \'<strong>Ręcznie wybrano:</strong> nie ujmuj w Souhrnné hlášení.\';\n        } else if (category.value === \'review\') {\n            hint.innerHTML = \'<strong>Ręcznie wybrano:</strong> faktura wymaga sprawdzenia.\';\n        } else if (taxMode.value === \'reverse_charge\' && eu.includes(prefix) && prefix !== \'CZ\') {\n            hint.innerHTML = `<strong>Automatycznie:</strong> VAT ID ${vat} wygląda na unijny. Faktura trafi do Souhrnné hlášení z kodem 3.`;\n        } else if (taxMode.value === \'reverse_charge\') {\n            hint.innerHTML = \'<strong>Uwaga:</strong> reverse charge, ale nie rozpoznano VAT ID z innego państwa UE. Po wystawieniu pojawi się ostrzeżenie.\';\n        } else {\n            hint.innerHTML = \'Ta faktura nie będzie automatycznie ujęta w Souhrnné hlášení.\';\n        }\n    }\n    function syncInvoiceRate() {\n        const currencyInput = document.getElementById(\'invoice-currency\');\n        const rate = document.getElementById(\'czk-rate\');\n        if (!currencyInput || !rate) return;\n        if (currencyInput.value.trim().toUpperCase() === \'CZK\' && !rate.value) rate.value = \'1\';\n    }\n    function updateExpensePreview() {\n        const amount = document.getElementById(\'expense-amount\');\n        const currencyInput = document.getElementById(\'expense-currency\');\n        const rate = document.getElementById(\'expense-rate\');\n        const preview = document.getElementById(\'expense-czk-preview\');\n        if (!amount || !currencyInput || !rate || !preview) return;\n        if (currencyInput.value.trim().toUpperCase() === \'CZK\' && !rate.value) rate.value = \'1\';\n        const value = parseNumber(amount.value) * parseNumber(rate.value);\n        preview.textContent = `${value.toFixed(2).replace(\'.\', \',\')} CZK`;\n    }\n    document.addEventListener(\'click\', (event) => {\n        const target = event.target;\n        if (!(target instanceof Element)) return;\n        if (target.id === \'add-item\') addItem();\n        if (target.classList.contains(\'remove-item\')) {\n            const rows = document.querySelectorAll(\'.invoice-item-row\');\n            if (rows.length <= 1) { alert(\'Faktura musi zawierać co najmniej jedną pozycję.\'); return; }\n            target.closest(\'.invoice-item-row\')?.remove(); recalculate();\n        }\n    });\n    document.addEventListener(\'input\', (event) => {\n        const target = event.target;\n        if (!(target instanceof Element)) return;\n        if (target.matches(\'.qty-input, .price-input, .vat-input, input[name="currency"]\')) recalculate();\n        if (target.matches(\'#invoice-currency\')) { syncInvoiceRate(); updateDphHint(); }\n        if (target.matches(\'#expense-amount, #expense-currency, #expense-rate\')) updateExpensePreview();\n    });\n    document.getElementById(\'tax-mode\')?.addEventListener(\'change\', updateTaxMode);\n    document.getElementById(\'contractor-select\')?.addEventListener(\'change\', updateDphHint);\n    document.getElementById(\'dph-category\')?.addEventListener(\'change\', updateDphHint);\n    const form = document.querySelector(\'.invoice-form\');\n    const issueDate = document.getElementById(\'issue-date\');\n    const dueDate = document.getElementById(\'due-date\');\n    if (form && issueDate && dueDate) {\n        let dueManuallyChanged = false;\n        dueDate.addEventListener(\'change\', () => { dueManuallyChanged = true; });\n        issueDate.addEventListener(\'change\', () => {\n            if (dueManuallyChanged || !issueDate.value) return;\n            const days = Number.parseInt(form.dataset.defaultDueDays || \'14\', 10);\n            const parsed = new Date(`${issueDate.value}T12:00:00`); parsed.setDate(parsed.getDate() + days);\n            dueDate.value = parsed.toISOString().slice(0, 10);\n        });\n    }\n    syncInvoiceRate(); updateTaxMode(); updateDphHint(); updateExpensePreview(); recalculate();\n})();\n'

# V4 template overrides.
EMBEDDED_TEMPLATES['dashboard.html'] = '{% extends "base.html" %}\n{% block title %}Start - Faktury OSVČ{% endblock %}\n{% block content %}\n<div class="page-header">\n    <div>\n        <h1>Panel działalności</h1>\n        <p class="muted">{{ company.name }} · IČO {{ company.company_id }} · DIČ {{ company.vat_id }}</p>\n    </div>\n    <a class="btn btn-primary" href="{{ url_for(\'invoice_new\') }}">Wystaw fakturę</a>\n</div>\n\n{% if dph_reminder %}\n<div class="callout warning">\n    <strong>DPH / UE:</strong> masz {{ dph_reminder.invoice_count }} fakturę/faktury do sprawdzenia za {{ dph_reminder.period_label }}.\n    Podstawowy termin: <strong>{{ dph_reminder.due_date|date_pl }}</strong>.\n    <a href="{{ url_for(\'dph_dashboard\', year=dph_reminder.year, month=dph_reminder.month) }}">Otwórz asystenta DPH</a>.\n</div>\n{% endif %}\n\n<div class="callout info">\n    <strong>Podatki i składki {{ tax_snapshot.year }}:</strong>\n    na podstawie zaksięgowanych faktur program szacuje, że należy odłożyć jeszcze\n    <strong>{{ tax_snapshot.remaining_total|money(\'CZK\') }}</strong>.\n    <a href="{{ url_for(\'taxes_dashboard\', year=tax_snapshot.year) }}">Zobacz wyliczenie i ustawienia</a>.\n</div>\n\n<div class="stats-grid dashboard-stats">\n    <div class="stat-card"><span class="stat-value compact-value">{{ tax_snapshot.revenue|money(\'CZK\') }}</span><span class="stat-label">Przychód podatkowy {{ tax_snapshot.year }}</span></div>\n    <div class="stat-card"><span class="stat-value compact-value">{{ tax_snapshot.total_obligation|money(\'CZK\') }}</span><span class="stat-label">Szacowany podatek + ČSSZ + VZP</span></div>\n    <div class="stat-card"><span class="stat-value compact-value">{{ tax_snapshot.after_obligations|money(\'CZK\') }}</span><span class="stat-label">Po pełnej rezerwie</span></div>\n    <div class="stat-card"><span class="stat-value">{{ counts.unpaid }}</span><span class="stat-label">Nieopłacone faktury</span></div>\n    <div class="stat-card"><span class="stat-value">{{ counts.invoices }}</span><span class="stat-label">Wszystkie faktury</span></div>\n    <div class="stat-card"><span class="stat-value">{{ counts.contractors }}</span><span class="stat-label">Kontrahenci</span></div>\n</div>\n\n<section class="panel">\n    <div class="panel-header"><h2>Ostatnie faktury</h2><a href="{{ url_for(\'invoices_list\') }}">Pokaż wszystkie</a></div>\n    {% if recent %}\n    <div class="table-wrap"><table>\n        <thead><tr><th>Numer</th><th>Kontrahent</th><th>Data</th><th>Kwota</th><th>Status</th><th></th></tr></thead>\n        <tbody>{% for invoice in recent %}<tr>\n            <td><a href="{{ url_for(\'invoice_view\', invoice_id=invoice.id) }}"><strong>{{ invoice.invoice_number }}</strong></a></td>\n            <td>{{ invoice.contractor_name }}</td><td>{{ invoice.issue_date|date_pl }}</td>\n            <td>{{ invoice.total|money(invoice.currency) }}</td>\n            <td><span class="badge badge-{{ invoice.status }}">{{ \'Opłacona\' if invoice.status == \'paid\' else \'Nieopłacona\' }}</span></td>\n            <td class="actions"><a class="btn btn-light btn-small" href="{{ url_for(\'invoice_pdf\', invoice_id=invoice.id) }}">PDF</a></td>\n        </tr>{% endfor %}</tbody>\n    </table></div>\n    {% else %}<div class="empty-state"><h3>Nie masz jeszcze faktur</h3><p>Dodaj kontrahenta, a następnie wystaw pierwszą fakturę.</p></div>{% endif %}\n</section>\n{% endblock %}\n'
EMBEDDED_TEMPLATES['taxes_dashboard.html'] = '{% extends "base.html" %}\n{% block title %}Podatki i składki - Faktury OSVČ{% endblock %}\n{% block content %}\n<div class="page-header">\n    <div><h1>Podatki i składki</h1><p class="muted">Bieżąca rezerwa na podatek dochodowy, ČSSZ i VZP.</p></div>\n    <form class="toolbar year-toolbar" method="get"><input type="number" name="year" min="2020" max="2100" value="{{ snapshot.year }}"><button class="btn btn-light" type="submit">Pokaż rok</button></form>\n</div>\n\n<div class="callout warning"><strong>Wyliczenie orientacyjne.</strong> Program liczy rezerwę z danych faktur i ustawień poniżej. Ostateczne kwoty wynikają z rocznego zeznania podatkowego oraz Přehledów dla ČSSZ i VZP.</div>\n{% if snapshot.year == 2026 %}\n<div class="callout info"><strong>Ustawiony przebieg 2026:</strong> działalność od 08.07.2026; ČSSZ i VZP bez minimum w lipcu z powodu zatrudnienia, a od 01.08.2026 działalność główna. Kalkulator uwzględnia {{ snapshot.cssz_secondary_months }} mies. vedlejší i {{ snapshot.cssz_main_months }} mies. hlavní.</div>\n{% endif %}\n{% if snapshot.warnings %}<div class="callout warning"><strong>Sprawdź:</strong><ul class="compact-list">{% for warning in snapshot.warnings %}<li>{{ warning }}</li>{% endfor %}</ul></div>{% endif %}\n\n<div class="stats-grid dashboard-stats tax-stats">\n    <div class="stat-card"><span class="stat-value compact-value">{{ snapshot.revenue|money(\'CZK\') }}</span><span class="stat-label">Przychód przyjęty do obliczeń</span></div>\n    <div class="stat-card"><span class="stat-value compact-value">{{ snapshot.flat_expenses|money(\'CZK\') }}</span><span class="stat-label">Paušální výdaje {{ snapshot.settings.expense_percent }}%</span></div>\n    <div class="stat-card"><span class="stat-value compact-value">{{ snapshot.profit|money(\'CZK\') }}</span><span class="stat-label">Szacowany zysk / dílčí základ</span></div>\n    <div class="stat-card"><span class="stat-value compact-value">{{ snapshot.income_tax|money(\'CZK\') }}</span><span class="stat-label">Podatek dochodowy do dopłaty</span></div>\n    <div class="stat-card"><span class="stat-value compact-value">{{ snapshot.cssz|money(\'CZK\') }}</span><span class="stat-label">Szacowane ČSSZ za rok</span></div>\n    <div class="stat-card"><span class="stat-value compact-value">{{ snapshot.vzp|money(\'CZK\') }}</span><span class="stat-label">Szacowane VZP za rok</span></div>\n    <div class="stat-card emphasis"><span class="stat-value compact-value">{{ snapshot.remaining_total|money(\'CZK\') }}</span><span class="stat-label">Do odłożenia teraz</span></div>\n    <div class="stat-card"><span class="stat-value compact-value">{{ snapshot.after_obligations|money(\'CZK\') }}</span><span class="stat-label">Przychód po pełnej rezerwie</span></div>\n</div>\n\n<section class="panel">\n    <div class="panel-header"><h2>Rozliczenie rezerwy {{ snapshot.year }}</h2><span class="badge badge-warning">{{ snapshot.active_months }} mies. działalności</span></div>\n    <div class="table-wrap"><table>\n        <thead><tr><th>Rodzaj</th><th>Szacowana należność</th><th>Zapisane wpłaty</th><th>Pozostało</th></tr></thead>\n        <tbody>\n            <tr><td><strong>Podatek dochodowy</strong></td><td>{{ snapshot.income_tax|money(\'CZK\') }}</td><td>{{ snapshot.paid.income_tax|money(\'CZK\') }}</td><td><strong>{{ snapshot.remaining.income_tax|money(\'CZK\') }}</strong></td></tr>\n            <tr><td><strong>ČSSZ</strong></td><td>{{ snapshot.cssz|money(\'CZK\') }}</td><td>{{ snapshot.paid.cssz|money(\'CZK\') }}</td><td><strong>{{ snapshot.remaining.cssz|money(\'CZK\') }}</strong></td></tr>\n            <tr><td><strong>VZP</strong></td><td>{{ snapshot.vzp|money(\'CZK\') }}</td><td>{{ snapshot.paid.vzp|money(\'CZK\') }}</td><td><strong>{{ snapshot.remaining.vzp|money(\'CZK\') }}</strong></td></tr>\n        </tbody>\n        <tfoot><tr><th>Razem</th><th>{{ snapshot.total_obligation|money(\'CZK\') }}</th><th>{{ snapshot.total_paid|money(\'CZK\') }}</th><th>{{ snapshot.remaining_total|money(\'CZK\') }}</th></tr></tfoot>\n    </table></div>\n    <div class="callout info"><strong>Bieżące zaliczki od 01.08.2026:</strong> ČSSZ {{ snapshot.settings.cssz_monthly_advance|money(\'CZK\') }} za dany miesiąc do jego ostatniego dnia; VZP {{ snapshot.settings.vzp_monthly_advance|money(\'CZK\') }} za dany miesiąc do 8. dnia następnego miesiąca. Każdą zapłatę zapisz niżej.</div>\n</section>\n\n<section class="panel">\n    <div class="panel-header"><h2>Faktury uwzględnione w przychodzie</h2><span class="muted">Podstawa: {{ \'zapłacone faktury\' if snapshot.settings.revenue_basis == \'paid\' else \'wystawione faktury\' }}</span></div>\n    {% if snapshot.invoice_rows %}<div class="table-wrap"><table>\n        <thead><tr><th>Faktura</th><th>Kontrahent</th><th>Status</th><th>Data przychodu</th><th>Kwota CZK</th></tr></thead>\n        <tbody>{% for row in snapshot.invoice_rows %}<tr>\n            <td><a href="{{ url_for(\'invoice_view\', invoice_id=row.id) }}"><strong>{{ row.invoice_number }}</strong></a></td><td>{{ row.contractor_name }}</td>\n            <td><span class="badge badge-{{ row.status }}">{{ \'Opłacona\' if row.status == \'paid\' else \'Nieopłacona\' }}</span></td>\n            <td>{{ row.recognized_date|date_pl }}</td><td>{{ row.value_czk|money(\'CZK\') }}</td>\n        </tr>{% endfor %}</tbody>\n    </table></div>{% else %}<div class="empty-state"><h3>Brak przychodu w tym roku</h3><p>Przy ustawieniu „zapłacone” faktura pojawi się po oznaczeniu jej jako opłaconej.</p></div>{% endif %}\n</section>\n\n<section class="panel">\n    <div class="panel-header"><h2>Zapisane wpłaty do urzędów</h2></div>\n    <form method="post" action="{{ url_for(\'tax_payment_add\') }}" class="form-grid four tax-payment-form">\n        <input type="hidden" name="year" value="{{ snapshot.year }}">\n        <label class="field"><span>Data wpłaty</span><input type="date" name="payment_date" value="{{ today }}" required></label>\n        <label class="field"><span>Rodzaj</span><select name="payment_type">{% for code, label in TAX_PAYMENT_TYPES.items() %}<option value="{{ code }}">{{ label }}</option>{% endfor %}</select></label>\n        <label class="field"><span>Kwota CZK</span><input type="number" step="0.01" min="0.01" name="amount" required></label>\n        <label class="field"><span>Notatka</span><input name="note" placeholder="np. zaliczka za sierpień"></label>\n        <div class="span-4 form-actions compact-actions"><button class="btn btn-primary" type="submit">Dodaj wpłatę</button></div>\n    </form>\n    {% if payments %}<div class="table-wrap"><table><thead><tr><th>Data</th><th>Rodzaj</th><th>Kwota</th><th>Notatka</th><th></th></tr></thead><tbody>\n    {% for payment in payments %}<tr><td>{{ payment.payment_date|date_pl }}</td><td>{{ TAX_PAYMENT_TYPES[payment.payment_type] }}</td><td>{{ payment.amount|money(\'CZK\') }}</td><td>{{ payment.note or \'—\' }}</td><td class="actions"><form method="post" action="{{ url_for(\'tax_payment_delete\', payment_id=payment.id) }}" onsubmit="return confirm(\'Usunąć tę wpłatę?\')"><input type="hidden" name="year" value="{{ snapshot.year }}"><button class="btn btn-light btn-small" type="submit">Usuń</button></form></td></tr>{% endfor %}\n    </tbody></table></div>{% else %}<p class="muted">Nie zapisano jeszcze żadnych wpłat.</p>{% endif %}\n</section>\n\n<details class="panel settings-panel" open>\n    <summary><strong>Ustawienia obliczeń {{ snapshot.year }}</strong></summary>\n    <form method="post" action="{{ url_for(\'tax_settings_save\') }}" class="form-panel tax-settings-form">\n        <input type="hidden" name="year" value="{{ snapshot.year }}">\n        <h3>Przychód i podatek</h3>\n        <div class="form-grid three">\n            <label class="field"><span>Początek działalności</span><input type="date" name="activity_start_date" value="{{ snapshot.settings.activity_start_date }}"></label>\n            <label class="field"><span>Koniec działalności (opcjonalnie)</span><input type="date" name="activity_end_date" value="{{ snapshot.settings.activity_end_date }}"></label>\n            <label class="field"><span>Do przychodu licz</span><select name="revenue_basis"><option value="paid" {% if snapshot.settings.revenue_basis == \'paid\' %}selected{% endif %}>Zapłacone faktury</option><option value="issued" {% if snapshot.settings.revenue_basis == \'issued\' %}selected{% endif %}>Wystawione faktury</option></select></label>\n            <label class="field"><span>Paušální výdaje (%)</span><input type="number" step="0.01" min="0" max="100" name="expense_percent" value="{{ snapshot.settings.expense_percent }}"></label>\n            <label class="field"><span>Maksymalny paušál CZK</span><input type="number" step="1" min="0" name="expense_limit" value="{{ snapshot.settings.expense_limit }}"></label>\n            <label class="field"><span>Roczna podstawowa ulga podatkowa</span><input type="number" step="1" min="0" name="income_tax_credit" value="{{ snapshot.settings.income_tax_credit }}"><small>Dla 2026 domyślnie 30 840 CZK.</small></label>\n            <label class="field"><span>Podstawa podatku z Accenture</span><input type="number" step="1" min="0" name="employment_tax_base" value="{{ snapshot.settings.employment_tax_base }}"><small>Przepisz z „Potvrzení o zdanitelných příjmech”.</small></label>\n            <label class="field"><span>Zaliczki podatku pobrane przez Accenture</span><input type="number" step="1" min="0" name="employment_tax_withheld" value="{{ snapshot.settings.employment_tax_withheld }}"><small>Także z „Potvrzení o zdanitelných příjmech”.</small></label>\n            <label class="field"><span>Próg 23% w CZK</span><input type="number" step="1" min="0" name="tax_threshold" value="{{ snapshot.settings.tax_threshold }}"></label>\n        </div>\n        <hr><h3>ČSSZ</h3>\n        <div class="form-grid three">\n            <label class="field"><span>Tryb działalności</span><select name="cssz_mode"><option value="secondary" {% if snapshot.settings.cssz_mode == \'secondary\' %}selected{% endif %}>Tylko vedlejší</option><option value="mixed" {% if snapshot.settings.cssz_mode == \'mixed\' %}selected{% endif %}>Zmiana vedlejší → hlavní w roku</option><option value="main" {% if snapshot.settings.cssz_mode == \'main\' %}selected{% endif %}>Tylko hlavní</option><option value="custom" {% if snapshot.settings.cssz_mode == \'custom\' %}selected{% endif %}>Własne ustawienia bez minimum</option></select></label>\n            <label class="field"><span>Hlavní od dnia</span><input type="date" name="cssz_main_from_date" value="{{ snapshot.settings.cssz_main_from_date }}"><small>U Ciebie 01.08.2026.</small></label>\n            <label class="field"><span>Rozhodná částka roczna</span><input type="number" step="1" name="cssz_threshold_annual" value="{{ snapshot.settings.cssz_threshold_annual }}"></label>\n            <label class="field"><span>Pomniejszenie za miesiąc bez vedlejší</span><input type="number" step="1" name="cssz_threshold_reduction_month" value="{{ snapshot.settings.cssz_threshold_reduction_month }}"></label>\n            <label class="field"><span>Podstawa z zysku (%)</span><input type="number" step="0.01" name="cssz_assessment_percent" value="{{ snapshot.settings.cssz_assessment_percent }}"></label>\n            <label class="field"><span>Stawka ČSSZ (%)</span><input type="number" step="0.01" name="cssz_rate_percent" value="{{ snapshot.settings.cssz_rate_percent }}"></label>\n            <label class="field"><span>Min. podstawa miesięczna hlavní</span><input type="number" step="0.01" name="cssz_min_monthly_base" value="{{ snapshot.settings.cssz_min_monthly_base }}"><small>17 139 CZK od lipca 2026.</small></label>\n            <label class="field"><span>Min. podstawa miesięczna vedlejší</span><input type="number" step="0.01" name="cssz_secondary_min_monthly_base" value="{{ snapshot.settings.cssz_secondary_min_monthly_base }}"></label>\n            <label class="field"><span>Aktualna zaliczka miesięczna ČSSZ</span><input type="number" step="0.01" min="0" name="cssz_monthly_advance" value="{{ snapshot.settings.cssz_monthly_advance }}"></label>\n        </div>\n        <hr><h3>VZP</h3>\n        <div class="form-grid three">\n            <label class="field"><span>Tryb ubezpieczenia</span><select name="vzp_mode"><option value="secondary" {% if snapshot.settings.vzp_mode == \'secondary\' %}selected{% endif %}>Tylko obok zatrudnienia / bez minimum</option><option value="mixed" {% if snapshot.settings.vzp_mode == \'mixed\' %}selected{% endif %}>Zmiana na główną OSVČ w roku</option><option value="main" {% if snapshot.settings.vzp_mode == \'main\' %}selected{% endif %}>Główna OSVČ przez cały okres</option><option value="custom" {% if snapshot.settings.vzp_mode == \'custom\' %}selected{% endif %}>Własne ustawienia bez minimum</option></select></label>\n            <label class="field"><span>Minimum VZP od dnia</span><input type="date" name="vzp_main_from_date" value="{{ snapshot.settings.vzp_main_from_date }}"><small>U Ciebie 01.08.2026.</small></label>\n            <label class="field"><span>Podstawa z zysku (%)</span><input type="number" step="0.01" name="vzp_assessment_percent" value="{{ snapshot.settings.vzp_assessment_percent }}"></label>\n            <label class="field"><span>Stawka VZP (%)</span><input type="number" step="0.01" name="vzp_rate_percent" value="{{ snapshot.settings.vzp_rate_percent }}"></label>\n            <label class="field"><span>Minimalna podstawa miesięczna</span><input type="number" step="0.01" name="vzp_min_monthly_base" value="{{ snapshot.settings.vzp_min_monthly_base }}"></label>\n            <label class="field"><span>Aktualna zaliczka miesięczna VZP</span><input type="number" step="0.01" min="0" name="vzp_monthly_advance" value="{{ snapshot.settings.vzp_monthly_advance }}"></label>\n        </div>\n        <div class="form-actions"><button class="btn btn-primary" type="submit">Zapisz ustawienia i przelicz</button></div>\n    </form>\n</details>\n{% endblock %}\n'
EMBEDDED_TEMPLATES['invoice_detail.html'] = '{% extends "base.html" %}\n{% block title %}{{ invoice.invoice_number }} - Faktury OSVČ{% endblock %}\n{% block content %}\n<div class="page-header"><div><h1>{{ invoice.invoice_number }}</h1><p class="muted">{{ invoice.contractor_name }} · {{ invoice.issue_date|date_pl }}</p></div><div class="button-row"><a class="btn btn-primary" href="{{ url_for(\'invoice_pdf\', invoice_id=invoice.id) }}">Pobierz PDF A4</a><a class="btn btn-light" href="{{ url_for(\'invoice_edit\', invoice_id=invoice.id) }}">Edytuj</a></div></div>\n{% if dph_info.kind == \'eu_service\' %}<div class="callout info"><strong>DPH / UE:</strong> ta faktura zostanie ujęta w Souhrnné hlášení za {{ dph_info.period_label }} z kodem plnění 3. Podstawowy termin: {{ dph_info.due_date|date_pl }}. <a href="{{ url_for(\'dph_dashboard\', year=invoice.supply_date[:4], month=invoice.supply_date[5:7]) }}">Otwórz raport</a>.</div>{% elif dph_info.warning %}<div class="callout warning"><strong>DPH do sprawdzenia:</strong> {{ dph_info.warning }}</div>{% endif %}\n<div class="detail-grid">\n<section class="panel"><div class="panel-header"><h2>Dane faktury</h2><span class="badge badge-{{ invoice.status }}">{{ \'Opłacona\' if invoice.status == \'paid\' else \'Nieopłacona\' }}</span></div><dl class="details">\n<div><dt>Kontrahent</dt><dd>{{ invoice.contractor_name }}</dd></div><div><dt>VAT ID</dt><dd>{{ invoice.contractor_vat_id or \'—\' }}</dd></div>\n<div><dt>Data wystawienia</dt><dd>{{ invoice.issue_date|date_pl }}</dd></div><div><dt>Data usługi</dt><dd>{{ invoice.supply_date|date_pl }}</dd></div>\n<div><dt>Termin płatności</dt><dd>{{ invoice.due_date|date_pl }}</dd></div><div><dt>Data zapłaty</dt><dd>{{ invoice.paid_date|date_pl if invoice.paid_date else \'—\' }}</dd></div>\n<div><dt>Tryb VAT</dt><dd>{{ TAX_MODES[invoice.tax_mode] }}</dd></div><div><dt>Kurs do CZK</dt><dd>{{ invoice.czk_rate or (\'1\' if invoice.currency == \'CZK\' else \'—\') }}</dd></div>\n<div><dt>DPH / VIES</dt><dd>{{ dph_info.label }}</dd></div></dl></section>\n<section class="panel total-card"><span class="muted">Do zapłaty</span><strong>{{ totals.total_gross|money(invoice.currency) }}</strong>{% if dph_info.value_czk is not none %}<small>Do ewidencji: {{ dph_info.value_czk|money(\'CZK\') }}</small>{% endif %}</section>\n</div>\n<section class="panel"><h2>Pozycje</h2><div class="table-wrap"><table><thead><tr><th>Opis</th><th>Ilość</th><th>Jedn.</th><th>Cena</th>{% if invoice.tax_mode == \'vat\' %}<th>VAT</th>{% endif %}<th>Wartość</th></tr></thead><tbody>{% for item in items %}<tr><td>{{ item.description }}</td><td>{{ item.quantity_decimal }}</td><td>{{ item.unit }}</td><td>{{ item.unit_price_decimal|money(invoice.currency) }}</td>{% if invoice.tax_mode == \'vat\' %}<td>{{ item.vat_rate_decimal }}%</td>{% endif %}<td>{{ item.gross|money(invoice.currency) }}</td></tr>{% endfor %}</tbody></table></div></section>\n{% if invoice.reverse_charge_note or invoice.notes %}<section class="panel">{% if invoice.reverse_charge_note %}<h2>Adnotacja</h2><p>{{ invoice.reverse_charge_note }}</p>{% endif %}{% if invoice.notes %}<h2>Uwagi</h2><p class="preline">{{ invoice.notes }}</p>{% endif %}</section>{% endif %}\n<div class="danger-zone paid-actions">\n    {% if invoice.status == \'paid\' %}\n    <form method="post" action="{{ url_for(\'invoice_toggle_paid\', invoice_id=invoice.id) }}"><button class="btn btn-light" type="submit">Oznacz jako nieopłaconą</button></form>\n    {% else %}\n    <form method="post" action="{{ url_for(\'invoice_toggle_paid\', invoice_id=invoice.id) }}" class="paid-date-form"><label class="field"><span>Data otrzymania zapłaty</span><input type="date" name="paid_date" value="{{ today }}" required></label><button class="btn btn-primary" type="submit">Oznacz jako opłaconą</button></form>\n    {% endif %}\n    <form method="post" action="{{ url_for(\'invoice_delete\', invoice_id=invoice.id) }}" onsubmit="return confirm(\'Usunąć fakturę {{ invoice.invoice_number }}?\')"><button class="btn btn-danger" type="submit">Usuń fakturę</button></form>\n</div>\n{% endblock %}\n'
EMBEDDED_CSS += '\n/* V4: podatek, ČSSZ i VZP */\n.tax-stats .emphasis { border-color: #d9b263; background: #fffaf0; }\n.form-grid.four { grid-template-columns: repeat(4, 1fr); }\n.span-4 { grid-column: span 4; }\n.year-toolbar { margin: 0; }\n.year-toolbar input { min-width: 120px; width: 120px; }\n.settings-panel summary { cursor: pointer; font-size: 18px; margin-bottom: 20px; }\n.tax-settings-form { margin-top: 20px; }\n.tax-payment-form { align-items: end; }\n.compact-actions { margin-top: 0; padding-top: 0; border-top: 0; }\n.paid-date-form { display: flex; align-items: end; gap: 12px; }\n.paid-date-form .field { min-width: 220px; }\n.paid-actions { align-items: end; }\ntfoot th { background: var(--primary-soft); }\n@media (max-width: 850px) {\n    .form-grid.four { grid-template-columns: 1fr; }\n    .span-4 { grid-column: span 1; }\n    .paid-date-form { width: 100%; flex-direction: column; align-items: stretch; }\n    .paid-date-form .field { min-width: 0; }\n}\n'

EMBEDDED_TEMPLATES['base.html'] = (
    EMBEDDED_TEMPLATES['base.html']
    .replace("<a href=\"{{ url_for('expenses_page') }}\">Koszty</a>", "<a href=\"{{ url_for('taxes_dashboard') }}\">Podatki i składki</a>")
    .replace("kontrahentów, faktur ani kosztów.", "kontrahentów, faktur ani ustawień podatków i składek.")
)
EMBEDDED_TEMPLATES['tools.html'] = (
    EMBEDDED_TEMPLATES['tools.html']
    .replace("Kontrahenci, koszty i dane firmy pozostaną.", "Kontrahenci, ustawienia podatków i składek oraz dane firmy pozostaną.")
)



# ---------------------------------------------------------------------------
# V6: osobna sekcja Ustawienia + czytelne rozliczenie podatków i składek
# ---------------------------------------------------------------------------

EMBEDDED_TEMPLATES["taxes_dashboard.html"] = r"""{% extends "base.html" %}
{% block title %}Podatki i składki - Faktury OSVČ{% endblock %}
{% block content %}
<div class="page-header">
    <div>
        <h1>Podatki i składki</h1>
        <p class="muted">Wyliczone zobowiązania, zapisane wpłaty i bieżące minima.</p>
    </div>
    <div class="button-row">
        <form class="toolbar year-toolbar" method="get">
            <input type="number" name="year" min="2020" max="2100" value="{{ snapshot.year }}">
            <button class="btn btn-light" type="submit">Pokaż rok</button>
        </form>
        <a class="btn btn-light" href="{{ url_for('settings_page', year=snapshot.year) }}">Ustawienia obliczeń</a>
    </div>
</div>

{% if snapshot.warnings %}
<div class="callout warning">
    <strong>Sprawdź:</strong>
    <ul class="compact-list">{% for warning in snapshot.warnings %}<li>{{ warning }}</li>{% endfor %}</ul>
</div>
{% endif %}

<div class="stats-grid dashboard-stats tax-stats">
    <div class="stat-card {% if snapshot.income_tax_overpayment > 0 %}tax-good{% elif snapshot.income_tax_underpayment > 0 %}tax-warning{% endif %}">
        <span class="stat-value compact-value">
            {% if snapshot.income_tax_overpayment > 0 %}
                {{ snapshot.income_tax_overpayment|money('CZK') }}
            {% else %}
                {{ snapshot.income_tax_underpayment|money('CZK') }}
            {% endif %}
        </span>
        <span class="stat-label">
            {% if snapshot.income_tax_overpayment > 0 %}Szacowana nadpłata podatku
            {% elif snapshot.income_tax_underpayment > 0 %}Szacowany podatek do dopłaty
            {% else %}Podatek rozliczony na zero{% endif %}
        </span>
    </div>
    <div class="stat-card">
        <span class="stat-value compact-value">{{ snapshot.remaining.cssz|money('CZK') }}</span>
        <span class="stat-label">ČSSZ pozostało wg szacunku</span>
    </div>
    <div class="stat-card">
        <span class="stat-value compact-value">{{ snapshot.remaining.vzp|money('CZK') }}</span>
        <span class="stat-label">VZP pozostało wg szacunku</span>
    </div>
    <div class="stat-card emphasis">
        <span class="stat-value compact-value">{{ snapshot.remaining_total|money('CZK') }}</span>
        <span class="stat-label">Łącznie do odłożenia / dopłaty</span>
    </div>
</div>

<section class="panel">
    <div class="panel-header">
        <h2>Minimalne miesięczne zaliczki</h2>
        {% if snapshot.year == 2026 %}<span class="badge badge-warning">hlavní od 01.08.2026</span>{% endif %}
    </div>
    <div class="minimum-grid">
        <div class="minimum-card">
            <span class="minimum-name">ČSSZ</span>
            <strong>{{ snapshot.settings.cssz_monthly_advance|money('CZK') }}</strong>
            <span class="muted">minimalna / bieżąca zaliczka miesięczna</span>
            <small>Za dany miesiąc do jego ostatniego dnia.</small>
        </div>
        <div class="minimum-card">
            <span class="minimum-name">VZP</span>
            <strong>{{ snapshot.settings.vzp_monthly_advance|money('CZK') }}</strong>
            <span class="muted">minimalna / bieżąca zaliczka miesięczna</span>
            <small>Za dany miesiąc do 8. dnia następnego miesiąca.</small>
        </div>
    </div>
</section>

<section class="panel">
    <div class="panel-header"><h2>Rozliczenie {{ snapshot.year }}</h2><span class="badge badge-warning">{{ snapshot.active_months }} mies. działalności</span></div>
    <div class="table-wrap"><table>
        <thead><tr><th>Rodzaj</th><th>Szacowana należność roczna</th><th>Zapłacono / pobrano</th><th>Bilans</th></tr></thead>
        <tbody>
            <tr>
                <td><strong>Podatek dochodowy</strong></td>
                <td>{{ snapshot.annual_tax_after_credit|money('CZK') }}</td>
                <td>{{ snapshot.income_tax_paid_total|money('CZK') }}<br><small class="muted">w tym Accenture {{ snapshot.employment_tax_withheld|money('CZK') }}</small></td>
                <td>
                    {% if snapshot.income_tax_overpayment > 0 %}
                        <strong class="text-success">Nadpłata {{ snapshot.income_tax_overpayment|money('CZK') }}</strong>
                    {% elif snapshot.income_tax_underpayment > 0 %}
                        <strong class="text-warning">Do dopłaty {{ snapshot.income_tax_underpayment|money('CZK') }}</strong>
                    {% else %}
                        <strong>0.00 CZK</strong>
                    {% endif %}
                </td>
            </tr>
            <tr>
                <td><strong>ČSSZ</strong></td>
                <td>{{ snapshot.cssz|money('CZK') }}</td>
                <td>{{ snapshot.paid.cssz|money('CZK') }}</td>
                <td><strong>{{ snapshot.remaining.cssz|money('CZK') }}</strong></td>
            </tr>
            <tr>
                <td><strong>VZP</strong></td>
                <td>{{ snapshot.vzp|money('CZK') }}</td>
                <td>{{ snapshot.paid.vzp|money('CZK') }}</td>
                <td><strong>{{ snapshot.remaining.vzp|money('CZK') }}</strong></td>
            </tr>
        </tbody>
    </table></div>
    <div class="callout info">
        <strong>Podstawa bieżącego wyliczenia:</strong>
        przychód {{ snapshot.revenue|money('CZK') }},
        paušální výdaje {{ snapshot.flat_expenses|money('CZK') }},
        szacowany zysk z OSVČ {{ snapshot.profit|money('CZK') }}.
        Dane wejściowe zmienisz w zakładce <a href="{{ url_for('settings_page', year=snapshot.year) }}">Ustawienia</a>.
    </div>
</section>

<section class="panel">
    <div class="panel-header"><h2>Zapisane wpłaty do urzędów</h2></div>
    <form method="post" action="{{ url_for('tax_payment_add') }}" class="form-grid four tax-payment-form">
        <input type="hidden" name="year" value="{{ snapshot.year }}">
        <label class="field"><span>Data wpłaty</span><input type="date" name="payment_date" value="{{ today }}" required></label>
        <label class="field"><span>Rodzaj</span><select name="payment_type">{% for code, label in TAX_PAYMENT_TYPES.items() %}<option value="{{ code }}">{{ label }}</option>{% endfor %}</select></label>
        <label class="field"><span>Kwota CZK</span><input type="number" step="0.01" min="0.01" name="amount" required></label>
        <label class="field"><span>Notatka</span><input name="note" placeholder="np. zaliczka za sierpień"></label>
        <div class="span-4 form-actions compact-actions"><button class="btn btn-primary" type="submit">Dodaj wpłatę</button></div>
    </form>
    {% if payments %}
    <div class="table-wrap"><table>
        <thead><tr><th>Data</th><th>Rodzaj</th><th>Kwota</th><th>Notatka</th><th></th></tr></thead>
        <tbody>
        {% for payment in payments %}
        <tr>
            <td>{{ payment.payment_date|date_pl }}</td>
            <td>{{ TAX_PAYMENT_TYPES[payment.payment_type] }}</td>
            <td>{{ payment.amount|money('CZK') }}</td>
            <td>{{ payment.note or '—' }}</td>
            <td class="actions"><form method="post" action="{{ url_for('tax_payment_delete', payment_id=payment.id) }}" onsubmit="return confirm('Usunąć tę wpłatę?')"><input type="hidden" name="year" value="{{ snapshot.year }}"><button class="btn btn-light btn-small" type="submit">Usuń</button></form></td>
        </tr>
        {% endfor %}
        </tbody>
    </table></div>
    {% else %}<p class="muted">Nie zapisano jeszcze żadnych wpłat.</p>{% endif %}
</section>
{% endblock %}
"""

EMBEDDED_TEMPLATES["settings.html"] = r"""{% extends "base.html" %}
{% block title %}Ustawienia - Faktury OSVČ{% endblock %}
{% block content %}
<div class="page-header">
    <div><h1>Ustawienia</h1><p class="muted">Dane podatkowe oraz parametry ČSSZ i VZP używane do obliczeń.</p></div>
    <form class="toolbar year-toolbar" method="get"><input type="number" name="year" min="2020" max="2100" value="{{ snapshot.year }}"><button class="btn btn-light" type="submit">Pokaż rok</button></form>
</div>
<div class="callout info">Zmiana tych wartości wpływa tylko na kalkulacje w aplikacji — nie wysyła żadnej zmiany do urzędu.</div>

<section class="panel">
<form method="post" action="{{ url_for('tax_settings_save') }}" class="form-panel tax-settings-form">
<input type="hidden" name="year" value="{{ snapshot.year }}">

<h2>Przychód i podatek</h2>
<div class="form-grid three">
<label class="field"><span>Początek działalności</span><input type="date" name="activity_start_date" value="{{ snapshot.settings.activity_start_date }}"></label>
<label class="field"><span>Koniec działalności (opcjonalnie)</span><input type="date" name="activity_end_date" value="{{ snapshot.settings.activity_end_date }}"></label>
<label class="field"><span>Do przychodu licz</span><select name="revenue_basis"><option value="paid" {% if snapshot.settings.revenue_basis == 'paid' %}selected{% endif %}>Zapłacone faktury / otrzymane płatności</option><option value="issued" {% if snapshot.settings.revenue_basis == 'issued' %}selected{% endif %}>Wystawione faktury</option></select></label>
<label class="field"><span>Paušální výdaje (%)</span><input type="number" step="0.01" min="0" max="100" name="expense_percent" value="{{ snapshot.settings.expense_percent }}"></label>
<label class="field"><span>Maksymalny paušál CZK</span><input type="number" step="1" min="0" name="expense_limit" value="{{ snapshot.settings.expense_limit }}"></label>
<label class="field"><span>Roczna podstawowa ulga podatkowa</span><input type="number" step="1" min="0" name="income_tax_credit" value="{{ snapshot.settings.income_tax_credit }}"></label>
<label class="field"><span>Podstawa podatku z Accenture</span><input type="number" step="1" min="0" name="employment_tax_base" value="{{ snapshot.settings.employment_tax_base }}"><small>Z „Potvrzení o zdanitelných příjmech”.</small></label>
<label class="field"><span>Zaliczki podatku pobrane przez Accenture</span><input type="number" step="1" min="0" name="employment_tax_withheld" value="{{ snapshot.settings.employment_tax_withheld }}"><small>Z tego samego dokumentu.</small></label>
<label class="field"><span>Próg 23% w CZK</span><input type="number" step="1" min="0" name="tax_threshold" value="{{ snapshot.settings.tax_threshold }}"></label>
</div>

<hr><h2>ČSSZ</h2>
<div class="form-grid three">
<label class="field"><span>Tryb działalności</span><select name="cssz_mode"><option value="secondary" {% if snapshot.settings.cssz_mode == 'secondary' %}selected{% endif %}>Tylko vedlejší</option><option value="mixed" {% if snapshot.settings.cssz_mode == 'mixed' %}selected{% endif %}>Zmiana vedlejší → hlavní w roku</option><option value="main" {% if snapshot.settings.cssz_mode == 'main' %}selected{% endif %}>Tylko hlavní</option><option value="custom" {% if snapshot.settings.cssz_mode == 'custom' %}selected{% endif %}>Własne ustawienia bez minimum</option></select></label>
<label class="field"><span>Hlavní od dnia</span><input type="date" name="cssz_main_from_date" value="{{ snapshot.settings.cssz_main_from_date }}"></label>
<label class="field"><span>Rozhodná částka roczna</span><input type="number" step="1" name="cssz_threshold_annual" value="{{ snapshot.settings.cssz_threshold_annual }}"></label>
<label class="field"><span>Pomniejszenie za miesiąc bez vedlejší</span><input type="number" step="1" name="cssz_threshold_reduction_month" value="{{ snapshot.settings.cssz_threshold_reduction_month }}"></label>
<label class="field"><span>Podstawa z zysku (%)</span><input type="number" step="0.01" name="cssz_assessment_percent" value="{{ snapshot.settings.cssz_assessment_percent }}"></label>
<label class="field"><span>Stawka ČSSZ (%)</span><input type="number" step="0.01" name="cssz_rate_percent" value="{{ snapshot.settings.cssz_rate_percent }}"></label>
<label class="field"><span>Min. podstawa miesięczna hlavní</span><input type="number" step="0.01" name="cssz_min_monthly_base" value="{{ snapshot.settings.cssz_min_monthly_base }}"></label>
<label class="field"><span>Min. podstawa miesięczna vedlejší</span><input type="number" step="0.01" name="cssz_secondary_min_monthly_base" value="{{ snapshot.settings.cssz_secondary_min_monthly_base }}"></label>
<label class="field"><span>Minimalna / bieżąca zaliczka miesięczna ČSSZ</span><input type="number" step="0.01" min="0" name="cssz_monthly_advance" value="{{ snapshot.settings.cssz_monthly_advance }}"></label>
</div>

<hr><h2>VZP</h2>
<div class="form-grid three">
<label class="field"><span>Tryb ubezpieczenia</span><select name="vzp_mode"><option value="secondary" {% if snapshot.settings.vzp_mode == 'secondary' %}selected{% endif %}>Tylko obok zatrudnienia / bez minimum</option><option value="mixed" {% if snapshot.settings.vzp_mode == 'mixed' %}selected{% endif %}>Zmiana na główną OSVČ w roku</option><option value="main" {% if snapshot.settings.vzp_mode == 'main' %}selected{% endif %}>Główna OSVČ przez cały okres</option><option value="custom" {% if snapshot.settings.vzp_mode == 'custom' %}selected{% endif %}>Własne ustawienia bez minimum</option></select></label>
<label class="field"><span>Minimum VZP od dnia</span><input type="date" name="vzp_main_from_date" value="{{ snapshot.settings.vzp_main_from_date }}"></label>
<label class="field"><span>Podstawa z zysku (%)</span><input type="number" step="0.01" name="vzp_assessment_percent" value="{{ snapshot.settings.vzp_assessment_percent }}"></label>
<label class="field"><span>Stawka VZP (%)</span><input type="number" step="0.01" name="vzp_rate_percent" value="{{ snapshot.settings.vzp_rate_percent }}"></label>
<label class="field"><span>Minimalna podstawa miesięczna</span><input type="number" step="0.01" name="vzp_min_monthly_base" value="{{ snapshot.settings.vzp_min_monthly_base }}"></label>
<label class="field"><span>Minimalna / bieżąca zaliczka miesięczna VZP</span><input type="number" step="0.01" min="0" name="vzp_monthly_advance" value="{{ snapshot.settings.vzp_monthly_advance }}"></label>
</div>

<div class="form-actions"><button class="btn btn-primary" type="submit">Zapisz ustawienia</button><a class="btn btn-light" href="{{ url_for('taxes_dashboard', year=snapshot.year) }}">Wróć do wyliczeń</a></div>
</form>
</section>
{% endblock %}
"""

_tools_link = """<a href="{{ url_for('tools_page') }}">Narzędzia</a>"""
_settings_and_tools = """<a href="{{ url_for('settings_page') }}">Ustawienia</a>
            <a href="{{ url_for('tools_page') }}">Narzędzia</a>"""
if "url_for('settings_page')" not in EMBEDDED_TEMPLATES["base.html"]:
    EMBEDDED_TEMPLATES["base.html"] = EMBEDDED_TEMPLATES["base.html"].replace(_tools_link, _settings_and_tools)

EMBEDDED_TEMPLATES["dashboard.html"] = EMBEDDED_TEMPLATES["dashboard.html"].replace("Zobacz wyliczenie i ustawienia", "Zobacz wyliczenie")

EMBEDDED_CSS += r"""
/* V6 */
.minimum-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px; }
.minimum-card { border:1px solid var(--border); border-radius:12px; padding:18px; display:flex; flex-direction:column; gap:5px; background:#fff; }
.minimum-card strong { font-size:25px; }
.minimum-name { font-weight:800; color:var(--primary); }
.tax-good { border-color:#9fd6b4 !important; background:#f2fbf5 !important; }
.tax-warning { border-color:#e8c47c !important; background:#fffaf0 !important; }
.text-success { color:#16843d; }
.text-warning { color:#a55a00; }
@media (max-width:760px) { .minimum-grid { grid-template-columns:1fr; } }
"""



# ---------------------------------------------------------------------------
# V7: szablony web/PWA, logowanie i projekty
# ---------------------------------------------------------------------------

EMBEDDED_TEMPLATES["login.html"] = r"""<!doctype html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#8b1538">
<title>Logowanie - Faktury OSVČ</title>
<style>
body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:#f4f6f8;margin:0;min-height:100vh;display:grid;place-items:center;color:#19202a}
.card{width:min(92vw,420px);background:#fff;border:1px solid #e1e5ea;border-radius:18px;padding:28px;box-shadow:0 15px 40px rgba(0,0,0,.08)}
h1{margin:0 0 8px;color:#8b1538}.muted{color:#68717d}.field{display:flex;flex-direction:column;gap:7px;margin:20px 0}.field input{font-size:18px;padding:13px;border:1px solid #ccd2d9;border-radius:10px}
button{width:100%;border:0;border-radius:10px;padding:13px;background:#8b1538;color:#fff;font-weight:700;font-size:16px}
.flash{padding:10px 12px;border-radius:8px;background:#fff0f0;color:#9d1c1c;margin-bottom:14px}
</style>
</head>
<body>
<div class="card">
<h1>Faktury OSVČ</h1>
<p class="muted">Zaloguj się do swojej aplikacji.</p>
{% with messages = get_flashed_messages(with_categories=true) %}
{% for category,message in messages %}<div class="flash">{{ message }}</div>{% endfor %}
{% endwith %}
<form method="post">
<label class="field"><span>Hasło</span><input type="password" name="password" autocomplete="current-password" autofocus required></label>
<button type="submit">Zaloguj</button>
</form>
</div>
</body>
</html>"""

EMBEDDED_TEMPLATES["base.html"] = r"""<!doctype html>
<html lang="pl">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
    <meta name="theme-color" content="#8b1538">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="default">
    <meta name="apple-mobile-web-app-title" content="Faktury OSVČ">
    <link rel="manifest" href="{{ url_for('manifest') }}">
    <link rel="icon" href="{{ url_for('app_icon') }}">
    <link rel="apple-touch-icon" href="{{ url_for('app_icon') }}">
    <title>{% block title %}Faktury OSVČ{% endblock %}</title>
    <link rel="stylesheet" href="{{ url_for('static', filename='style.css') }}">
</head>
<body>
<header class="topbar">
    <div class="topbar-inner">
        <a class="brand" href="{{ url_for('dashboard') }}">Faktury OSVČ</a>
        <button class="mobile-menu-button" id="mobile-menu-button" type="button" aria-label="Menu">☰</button>
        <nav class="nav" id="main-nav">
            <a href="{{ url_for('dashboard') }}">Start</a>
            <a href="{{ url_for('invoices_list') }}">Faktury</a>
            <a href="{{ url_for('contractors_list') }}">Kontrahenci</a>
            <a href="{{ url_for('projects_list') }}">Projekty</a>
            <a href="{{ url_for('taxes_dashboard') }}">Podatki i składki</a>
            <a href="{{ url_for('dph_dashboard') }}">DPH / UE</a>
            <a href="{{ url_for('company_edit') }}">Moja firma</a>
            <a href="{{ url_for('settings_page') }}">Ustawienia</a>
            <a href="{{ url_for('tools_page') }}">Narzędzia</a>
            {% if auth_enabled %}<a href="{{ url_for('logout') }}">Wyloguj</a>{% endif %}
        </nav>
        <a class="btn btn-primary btn-small desktop-new-invoice" href="{{ url_for('invoice_new') }}">+ Nowa faktura</a>
    </div>
</header>

<main class="container">
    {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}
            <div class="flash-stack">
                {% for category, message in messages %}
                    <div class="flash flash-{{ category }}">{{ message }}</div>
                {% endfor %}
            </div>
        {% endif %}
    {% endwith %}
    {% block content %}{% endblock %}
</main>

<a class="mobile-fab" href="{{ url_for('invoice_new') }}" aria-label="Nowa faktura">＋</a>
<script src="{{ url_for('static', filename='app.js') }}"></script>
<script>
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => navigator.serviceWorker.register('{{ url_for("service_worker") }}').catch(()=>{}));
}
</script>
</body>
</html>"""

EMBEDDED_TEMPLATES["projects_list.html"] = r"""{% extends "base.html" %}
{% block title %}Projekty - Faktury OSVČ{% endblock %}
{% block content %}
<div class="page-header">
  <div><h1>Projekty</h1><p class="muted">Zapisz numery projektów raz i wybieraj je później przy wystawianiu faktur.</p></div>
  <a class="btn btn-primary" href="{{ url_for('project_new') }}">+ Dodaj projekt</a>
</div>
<section class="panel">
{% if projects %}
<div class="table-wrap"><table>
<thead><tr><th>Numer</th><th>Nazwa</th><th>Kontrahent</th><th>Status</th><th></th></tr></thead>
<tbody>
{% for p in projects %}
<tr>
<td><strong>{{ p.project_number }}</strong></td>
<td>{{ p.project_name or '—' }}</td>
<td>{{ p.contractor_name or 'Wszyscy / bez przypisania' }}</td>
<td>{% if p.active %}<span class="badge badge-paid">Aktywny</span>{% else %}<span class="badge">Nieaktywny</span>{% endif %}</td>
<td class="actions"><a class="btn btn-light btn-small" href="{{ url_for('project_edit', project_id=p.id) }}">Edytuj</a>
<form class="inline-form" method="post" action="{{ url_for('project_delete', project_id=p.id) }}" onsubmit="return confirm('Usunąć projekt {{ p.project_number }}?')"><button class="btn btn-light btn-small" type="submit">Usuń</button></form></td>
</tr>
{% endfor %}
</tbody></table></div>
{% else %}
<div class="empty-state"><h2>Brak zapisanych projektów</h2><p>Dodaj pierwszy numer projektu.</p><a class="btn btn-primary" href="{{ url_for('project_new') }}">Dodaj projekt</a></div>
{% endif %}
</section>
{% endblock %}"""

EMBEDDED_TEMPLATES["project_form.html"] = r"""{% extends "base.html" %}
{% block title %}{{ 'Edytuj projekt' if is_edit else 'Nowy projekt' }} - Faktury OSVČ{% endblock %}
{% block content %}
<div class="page-header"><div><h1>{{ 'Edytuj projekt' if is_edit else 'Nowy projekt' }}</h1></div></div>
<form method="post" class="panel form-panel">
<div class="form-grid two">
<label class="field"><span>Numer projektu *</span><input name="project_number" value="{{ project.project_number }}" required autofocus placeholder="np. HK348"></label>
<label class="field"><span>Kontrahent</span><select name="contractor_id"><option value="">— bez przypisania —</option>{% for c in contractors %}<option value="{{ c.id }}" {% if project.contractor_id|string == c.id|string %}selected{% endif %}>{{ c.name }}</option>{% endfor %}</select></label>
<label class="field span-2"><span>Nazwa / opis projektu</span><input name="project_name" value="{{ project.project_name }}" placeholder="np. Vienna - modernization"></label>
<label class="field span-2"><span>Notatki</span><textarea name="notes" rows="3">{{ project.notes }}</textarea></label>
<label class="checkbox-row span-2"><input type="checkbox" name="active" value="1" {% if project.active %}checked{% endif %}> <span>Projekt aktywny</span></label>
</div>
<div class="form-actions"><button class="btn btn-primary" type="submit">Zapisz</button><a class="btn btn-ghost" href="{{ url_for('projects_list') }}">Anuluj</a></div>
</form>
{% endblock %}"""

# Dodaj wybór projektu do formularza faktury.
_invoice_form = EMBEDDED_TEMPLATES["invoice_form.html"]
_order_field = """        <label class="field"><span>Numer zamówienia</span><input name="order_number" value="{{ invoice.order_number }}"></label>
"""
_project_fields = """        <label class="field"><span>Projekt</span>
            <select id="project-select">
                <option value="">— wybierz zapisany projekt —</option>
                {% for project in projects %}
                <option value="{{ project.project_number }}" data-contractor="{{ project.contractor_id or '' }}" {% if invoice.order_number == project.project_number %}selected{% endif %}>{{ project.project_number }}{% if project.project_name %} — {{ project.project_name }}{% endif %}</option>
                {% endfor %}
            </select>
            <small>Lista filtruje się po wybranym kontrahencie.</small>
        </label>
        <label class="field"><span>Numer projektu / zamówienia</span><input id="order-number" name="order_number" value="{{ invoice.order_number }}" placeholder="Wybierz projekt lub wpisz ręcznie"></label>
"""
if _order_field in _invoice_form:
    _invoice_form = _invoice_form.replace(_order_field, _project_fields, 1)
else:
    raise RuntimeError("Nie znaleziono pola numeru zamówienia w invoice_form.")
EMBEDDED_TEMPLATES["invoice_form.html"] = _invoice_form

# Mobilny interfejs.
EMBEDDED_CSS += r"""
/* V7 mobile / PWA */
.mobile-menu-button,.mobile-fab{display:none}
@media (max-width: 900px){
  body{padding-bottom:78px}
  .container{padding:16px 12px}
  .topbar-inner{min-height:58px;position:relative;padding:0 12px}
  .mobile-menu-button{display:inline-flex;border:0;background:transparent;font-size:27px;color:var(--primary);padding:6px 10px;cursor:pointer}
  .desktop-new-invoice{display:none}
  .nav{display:none;position:absolute;left:10px;right:10px;top:58px;z-index:1000;background:#fff;border:1px solid var(--border);border-radius:14px;padding:8px;box-shadow:0 16px 40px rgba(0,0,0,.14);flex-direction:column;align-items:stretch}
  .nav.is-open{display:flex}
  .nav a{padding:11px 12px;border-radius:9px}
  .nav a:hover{background:#f6f1f3}
  .page-header{align-items:flex-start;gap:12px;flex-direction:column}
  .page-header>.button-row,.button-row{width:100%;flex-wrap:wrap}
  .form-grid.two,.form-grid.three,.form-grid.four,.stats-grid,.detail-grid,.minimum-grid{grid-template-columns:1fr!important}
  .span-2,.span-3,.span-4{grid-column:auto!important}
  .panel{padding:16px 12px;border-radius:14px}
  .table-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
  table{min-width:660px}
  .invoice-items{min-width:760px}
  input,select,textarea{font-size:16px!important}
  .mobile-fab{display:flex;position:fixed;right:18px;bottom:18px;width:56px;height:56px;border-radius:50%;background:var(--primary);color:#fff;align-items:center;justify-content:center;font-size:31px;text-decoration:none;box-shadow:0 10px 26px rgba(0,0,0,.25);z-index:900}
}
@media (display-mode: standalone){
  .topbar{padding-top:env(safe-area-inset-top)}
}
"""

# Rozszerz JS o menu i projekty.
EMBEDDED_JS += r"""
document.addEventListener('DOMContentLoaded', function(){
  const menuButton = document.getElementById('mobile-menu-button');
  const nav = document.getElementById('main-nav');
  if(menuButton && nav){
    menuButton.addEventListener('click', ()=>nav.classList.toggle('is-open'));
    nav.querySelectorAll('a').forEach(a=>a.addEventListener('click',()=>nav.classList.remove('is-open')));
  }

  const contractor = document.getElementById('contractor-select');
  const project = document.getElementById('project-select');
  const order = document.getElementById('order-number');

  function filterProjects(){
    if(!project) return;
    const cid = contractor ? String(contractor.value || '') : '';
    Array.from(project.options).forEach((opt, idx)=>{
      if(idx===0){ opt.hidden=false; return; }
      const pcid = String(opt.dataset.contractor || '');
      opt.hidden = !!pcid && !!cid && pcid !== cid;
    });
    const selected = project.options[project.selectedIndex];
    if(selected && selected.hidden) project.value = '';
  }
  if(contractor){ contractor.addEventListener('change', filterProjects); }
  if(project){
    project.addEventListener('change', function(){
      if(order && project.value) order.value = project.value;
    });
    filterProjects();
  }
});
"""


APP_DIR = Path(__file__).resolve().parent


def _default_data_dir() -> Path:
    if CLOUD_MODE:
        return Path("/tmp") / "FakturyOSVC"
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
        return base / "FakturyOSVC"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "FakturyOSVC"
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "FakturyOSVC"


DATA_DIR = Path(os.environ.get("INVOICE_APP_DATA", str(_default_data_dir())))
DATA_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_DIR = DATA_DIR / "backups"
BACKUP_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = Path(os.environ.get("INVOICE_APP_DB", str(DATA_DIR / "invoice_app.db")))

# PDF-y są przechowywane w łatwo dostępnym katalogu Dokumenty.
# Strukturę tworzy program automatycznie:
# Dokumenty/Faktury OSVC/Nazwa kontrahenta/Rok/Faktura_....pdf
DEFAULT_PDF_DIR = (DATA_DIR / "invoices") if CLOUD_MODE else (Path.home() / "Documents" / "Faktury OSVC")
INVOICE_PDF_DIR = Path(os.environ.get("INVOICE_PDF_DIR", str(DEFAULT_PDF_DIR)))
INVOICE_PDF_DIR.mkdir(parents=True, exist_ok=True)



_REMOTE_SYNC_LOCK = threading.RLock()


def _supabase_object_url(object_path: str, authenticated: bool = False) -> str:
    safe_bucket = urllib.parse.quote(SUPABASE_BUCKET, safe="")
    safe_path = urllib.parse.quote(object_path.lstrip("/"), safe="/")
    if authenticated:
        return f"{SUPABASE_URL}/storage/v1/object/authenticated/{safe_bucket}/{safe_path}"
    return f"{SUPABASE_URL}/storage/v1/object/{safe_bucket}/{safe_path}"


def _supabase_headers(content_type: str | None = None) -> dict[str, str]:
    """
    Obsługuje oba formaty kluczy Supabase:
    - nowy secret key: sb_secret_... -> tylko nagłówek apikey
    - legacy service_role JWT: eyJ... -> apikey + Authorization Bearer
    """
    headers = {"apikey": SUPABASE_SERVICE_KEY}
    if not SUPABASE_SERVICE_KEY.startswith("sb_secret_"):
        headers["Authorization"] = f"Bearer {SUPABASE_SERVICE_KEY}"
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def download_remote_database() -> bool:
    """Pobiera najnowszą bazę z prywatnego Supabase Storage przy starcie."""
    if not REMOTE_DB_ENABLED:
        return False
    with _REMOTE_SYNC_LOCK:
        req = urllib.request.Request(
            _supabase_object_url(SUPABASE_DB_OBJECT, authenticated=True),
            headers=_supabase_headers(),
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                data = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                print("Supabase: brak zdalnej bazy — przy pierwszym uruchomieniu zostanie utworzona nowa.")
                return False
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            print(f"Supabase: nie udało się pobrać bazy (HTTP {exc.code}): {body}")
            return False
        except Exception as exc:
            print(f"Supabase: nie udało się pobrać bazy: {exc}")
            return False

        if not data:
            return False
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp_path = DB_PATH.with_suffix(".download")
        temp_path.write_bytes(data)
        # Szybka kontrola, czy to rzeczywiście poprawna baza SQLite.
        try:
            check = sqlite3.connect(temp_path)
            check.execute("PRAGMA schema_version").fetchone()
            check.close()
        except Exception as exc:
            try:
                temp_path.unlink()
            except OSError:
                pass
            print(f"Supabase: pobrany plik nie jest poprawną bazą SQLite: {exc}")
            return False
        os.replace(temp_path, DB_PATH)
        print(f"Supabase: pobrano bazę ({len(data)} bajtów).")
        return True


def _sqlite_snapshot_file() -> Path:
    """Tworzy spójny snapshot otwartej/aktywnej bazy SQLite."""
    fd, temp_name = tempfile.mkstemp(prefix="faktury_cloud_", suffix=".sqlite3")
    os.close(fd)
    target = Path(temp_name)
    source = sqlite3.connect(DB_PATH)
    destination = sqlite3.connect(target)
    try:
        with destination:
            source.backup(destination)
    finally:
        destination.close()
        source.close()
    return target


def upload_file_to_supabase(local_path: Path, object_path: str) -> bool:
    if not REMOTE_DB_ENABLED or not local_path.exists():
        return False
    with _REMOTE_SYNC_LOCK:
        data = local_path.read_bytes()
        req = urllib.request.Request(
            _supabase_object_url(object_path, authenticated=False),
            data=data,
            headers={
                **_supabase_headers("application/octet-stream"),
                "x-upsert": "true",
                "cache-control": "no-cache",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=45) as response:
                response.read()
            return True
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            print(f"Supabase: upload nieudany HTTP {exc.code}: {body}")
            return False
        except Exception as exc:
            print(f"Supabase: upload nieudany: {exc}")
            return False


def upload_remote_database() -> bool:
    """Wysyła spójny snapshot bazy po każdej zmianie."""
    if not REMOTE_DB_ENABLED or not DB_PATH.exists():
        return False
    snapshot = None
    try:
        snapshot = _sqlite_snapshot_file()
        ok = upload_file_to_supabase(snapshot, SUPABASE_DB_OBJECT)
        if ok:
            print("Supabase: baza zsynchronizowana.")
        return ok
    finally:
        if snapshot is not None:
            try:
                snapshot.unlink()
            except OSError:
                pass


# Na darmowym Renderze lokalny dysk znika po uśpieniu/restarcie.
# Dlatego pobieramy bazę zanim uruchomimy migracje i init_db().
REMOTE_DB_DOWNLOADED = download_remote_database()

def _migrate_legacy_database() -> str | None:
    """Kopiuje bazę ze starej wieloplikowej wersji przy pierwszym uruchomieniu."""
    if DB_PATH.exists():
        return None
    candidates = [
        APP_DIR / "data" / "invoice_app.db",
        APP_DIR / "invoice_app.db",
        Path.cwd() / "data" / "invoice_app.db",
        Path.cwd() / "invoice_app.db",
    ]
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen or resolved == DB_PATH.resolve():
            continue
        seen.add(resolved)
        if candidate.is_file():
            shutil.copy2(candidate, DB_PATH)
            return str(candidate)
    return None


MIGRATED_FROM = _migrate_legacy_database()

app = Flask(__name__, static_folder=None)
app.secret_key = SECRET_KEY
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax', SESSION_COOKIE_SECURE=CLOUD_MODE)
app.jinja_loader = DictLoader(EMBEDDED_TEMPLATES)
app.secret_key = SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024


@app.route("/static/<path:filename>", endpoint="static")
def embedded_static(filename: str):
    if filename == "style.css":
        return Response(EMBEDDED_CSS, mimetype="text/css")
    if filename == "app.js":
        return Response(EMBEDDED_JS, mimetype="application/javascript")
    abort(404)

TWOPLACES = Decimal("0.01")

LANGUAGES = {
    "pl": "Polski",
    "de": "Deutsch",
    "en": "English",
    "cs": "Čeština",
}

TAX_MODES = {
    "reverse_charge": "Reverse charge (odwrotne obciążenie)",
    "no_vat": "Bez VAT",
    "vat": "VAT naliczany",
}

PAYMENT_METHODS = {
    "bank_transfer": "Przelew bankowy",
    "cash": "Gotówka",
    "card": "Karta",
}


DPH_CATEGORIES = {
    "auto": "Automatycznie",
    "eu_service": "Usługa B2B w UE - SH VIES, kod 3",
    "not_required": "Nie ujmuj w SH VIES",
    "review": "Do ręcznego sprawdzenia",
}

EXPENSE_CATEGORIES = {
    "tools": "Narzędzia i sprzęt",
    "materials": "Materiały",
    "transport": "Transport i paliwo",
    "rental": "Wynajem sprzętu / lokalu",
    "phone": "Telefon i internet",
    "accounting": "Księgowość",
    "insurance": "Ubezpieczenia",
    "fees": "Opłaty urzędowe",
    "software": "Oprogramowanie i reklama",
    "other": "Inne",
}

EXPENSE_DPH_OBLIGATIONS = {
    "none": "Brak szczególnego obowiązku DPH",
    "foreign_service": "Usługa kupiona z zagranicy - możliwe přiznání k DPH",
    "review": "Do ręcznego sprawdzenia",
}

TAX_PAYMENT_TYPES = {
    'income_tax': 'Podatek dochodowy',
    'cssz': 'ČSSZ',
    'vzp': 'VZP',
}

MONTHS = {
    1: "Styczeń", 2: "Luty", 3: "Marzec", 4: "Kwiecień", 5: "Maj", 6: "Czerwiec",
    7: "Lipiec", 8: "Sierpień", 9: "Wrzesień", 10: "Październik", 11: "Listopad", 12: "Grudzień",
}

EU_VAT_PREFIXES = {
    "AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "EL", "ES", "FI", "FR",
    "HR", "HU", "IE", "IT", "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO",
    "SE", "SI", "SK",
}

MOJE_DANE_SHV_URL = "https://adisspr.mfcr.cz/pmd/epo/novy/DPH_SHV"
VIES_URL = "https://ec.europa.eu/taxation_customs/vies/"

PDF_PAYMENT_METHODS = {
    "pl": {"bank_transfer": "Przelew bankowy", "cash": "Gotówka", "card": "Karta"},
    "de": {"bank_transfer": "Banküberweisung", "cash": "Barzahlung", "card": "Kartenzahlung"},
    "en": {"bank_transfer": "Bank transfer", "cash": "Cash", "card": "Card"},
    "cs": {"bank_transfer": "Bankovní převod", "cash": "Hotově", "card": "Kartou"},
}

PDF_LABELS: dict[str, dict[str, str]] = {
    "pl": {
        "invoice": "FAKTURA",
        "invoice_no": "Numer faktury",
        "seller": "Sprzedawca",
        "buyer": "Nabywca",
        "issue_date": "Data wystawienia",
        "supply_date": "Data wykonania usługi",
        "due_date": "Termin płatności",
        "order_no": "Numer zamówienia",
        "payment_method": "Sposób płatności",
        "variable_symbol": "Symbol płatności",
        "item_no": "Lp.",
        "description": "Opis",
        "quantity": "Ilość",
        "unit": "Jedn.",
        "unit_price": "Cena jedn.",
        "vat_rate": "VAT %",
        "net": "Netto",
        "vat": "VAT",
        "gross": "Brutto",
        "total_due": "Do zapłaty",
        "bank_details": "Dane do płatności",
        "bank": "Bank",
        "account": "Rachunek",
        "iban": "IBAN",
        "bic": "BIC/SWIFT",
        "notes": "Uwagi",
        "page": "Strona",
        "company_id": "IČO / nr firmy",
        "vat_id": "DIČ / VAT ID",
        "email": "E-mail",
        "phone": "Telefon",
        "reverse_charge_default": "Odwrotne obciążenie - VAT rozlicza nabywca. Reverse charge.",
        "tax_mode": "Rozliczenie podatku",
        "reverse_charge": "Reverse charge",
        "no_vat": "Bez VAT",
    },
    "de": {
        "invoice": "RECHNUNG",
        "invoice_no": "Rechnungsnummer",
        "seller": "Leistungserbringer",
        "buyer": "Leistungsempfänger",
        "issue_date": "Rechnungsdatum",
        "supply_date": "Leistungsdatum",
        "due_date": "Fälligkeitsdatum",
        "order_no": "Bestellnummer",
        "payment_method": "Zahlungsart",
        "variable_symbol": "Zahlungsreferenz",
        "item_no": "Pos.",
        "description": "Beschreibung",
        "quantity": "Menge",
        "unit": "Einheit",
        "unit_price": "Einzelpreis",
        "vat_rate": "USt. %",
        "net": "Netto",
        "vat": "USt.",
        "gross": "Brutto",
        "total_due": "Zahlbetrag",
        "bank_details": "Bankverbindung",
        "bank": "Bank",
        "account": "Kontonummer",
        "iban": "IBAN",
        "bic": "BIC/SWIFT",
        "notes": "Hinweise",
        "page": "Seite",
        "company_id": "IČO / Firmen-ID",
        "vat_id": "UID / VAT ID",
        "email": "E-Mail",
        "phone": "Telefon",
        "reverse_charge_default": "Steuerschuldnerschaft des Leistungsempfängers (Reverse Charge).",
        "tax_mode": "Umsatzsteuer",
        "reverse_charge": "Reverse Charge",
        "no_vat": "Ohne Umsatzsteuer",
    },
    "en": {
        "invoice": "INVOICE",
        "invoice_no": "Invoice number",
        "seller": "Supplier",
        "buyer": "Customer",
        "issue_date": "Issue date",
        "supply_date": "Service date",
        "due_date": "Due date",
        "order_no": "Order number",
        "payment_method": "Payment method",
        "variable_symbol": "Payment reference",
        "item_no": "No.",
        "description": "Description",
        "quantity": "Qty",
        "unit": "Unit",
        "unit_price": "Unit price",
        "vat_rate": "VAT %",
        "net": "Net",
        "vat": "VAT",
        "gross": "Gross",
        "total_due": "Amount due",
        "bank_details": "Payment details",
        "bank": "Bank",
        "account": "Account",
        "iban": "IBAN",
        "bic": "BIC/SWIFT",
        "notes": "Notes",
        "page": "Page",
        "company_id": "Company ID",
        "vat_id": "VAT ID",
        "email": "Email",
        "phone": "Phone",
        "reverse_charge_default": "Reverse charge - VAT is payable by the recipient.",
        "tax_mode": "VAT treatment",
        "reverse_charge": "Reverse charge",
        "no_vat": "No VAT",
    },
    "cs": {
        "invoice": "FAKTURA",
        "invoice_no": "Číslo faktury",
        "seller": "Dodavatel",
        "buyer": "Odběratel",
        "issue_date": "Datum vystavení",
        "supply_date": "Datum poskytnutí služby",
        "due_date": "Datum splatnosti",
        "order_no": "Číslo objednávky",
        "payment_method": "Způsob úhrady",
        "variable_symbol": "Variabilní symbol",
        "item_no": "Č.",
        "description": "Popis",
        "quantity": "Množství",
        "unit": "Jedn.",
        "unit_price": "Jedn. cena",
        "vat_rate": "DPH %",
        "net": "Základ",
        "vat": "DPH",
        "gross": "Celkem",
        "total_due": "K úhradě",
        "bank_details": "Platební údaje",
        "bank": "Banka",
        "account": "Účet",
        "iban": "IBAN",
        "bic": "BIC/SWIFT",
        "notes": "Poznámky",
        "page": "Strana",
        "company_id": "IČO",
        "vat_id": "DIČ",
        "email": "E-mail",
        "phone": "Telefon",
        "reverse_charge_default": "Daň odvede zákazník (reverse charge).",
        "tax_mode": "Režim DPH",
        "reverse_charge": "Přenesení daňové povinnosti",
        "no_vat": "Bez DPH",
    },
}


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(DB_PATH)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        g.db = connection
    return g.db


@app.teardown_appcontext
def close_db(_exception: BaseException | None = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS company (
            id INTEGER PRIMARY KEY CHECK (id = 1), name TEXT NOT NULL,
            address TEXT NOT NULL DEFAULT '', postal_code TEXT NOT NULL DEFAULT '',
            city TEXT NOT NULL DEFAULT '', country TEXT NOT NULL DEFAULT '',
            company_id TEXT NOT NULL DEFAULT '', vat_id TEXT NOT NULL DEFAULT '',
            email TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '',
            bank_name TEXT NOT NULL DEFAULT '', bank_account TEXT NOT NULL DEFAULT '',
            iban TEXT NOT NULL DEFAULT '', bic TEXT NOT NULL DEFAULT '',
            default_currency TEXT NOT NULL DEFAULT 'EUR', default_due_days INTEGER NOT NULL DEFAULT 14,
            invoice_prefix TEXT NOT NULL DEFAULT 'FV', default_language TEXT NOT NULL DEFAULT 'de',
            default_tax_mode TEXT NOT NULL DEFAULT 'reverse_charge',
            default_reverse_charge_note TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS contractors (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            address TEXT NOT NULL DEFAULT '', postal_code TEXT NOT NULL DEFAULT '', city TEXT NOT NULL DEFAULT '',
            country TEXT NOT NULL DEFAULT '', company_id TEXT NOT NULL DEFAULT '', vat_id TEXT NOT NULL DEFAULT '',
            email TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contractor_id INTEGER,
            project_number TEXT NOT NULL,
            project_name TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (contractor_id) REFERENCES contractors(id) ON DELETE SET NULL,
            UNIQUE(contractor_id, project_number)
        );
        CREATE TABLE IF NOT EXISTS invoice_sequences (year INTEGER PRIMARY KEY, next_number INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_number TEXT NOT NULL UNIQUE,
            invoice_year INTEGER NOT NULL, sequence_no INTEGER NOT NULL, contractor_id INTEGER NOT NULL,
            issue_date TEXT NOT NULL, supply_date TEXT NOT NULL, due_date TEXT NOT NULL,
            currency TEXT NOT NULL DEFAULT 'EUR', language TEXT NOT NULL DEFAULT 'de',
            tax_mode TEXT NOT NULL DEFAULT 'reverse_charge', payment_method TEXT NOT NULL DEFAULT 'bank_transfer',
            variable_symbol TEXT NOT NULL DEFAULT '', order_number TEXT NOT NULL DEFAULT '',
            reverse_charge_note TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'unpaid', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (contractor_id) REFERENCES contractors(id) ON DELETE RESTRICT
        );
        CREATE TABLE IF NOT EXISTS invoice_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_id INTEGER NOT NULL, position INTEGER NOT NULL,
            description TEXT NOT NULL, quantity TEXT NOT NULL, unit TEXT NOT NULL DEFAULT 'h',
            unit_price TEXT NOT NULL, vat_rate TEXT NOT NULL DEFAULT '0',
            FOREIGN KEY (invoice_id) REFERENCES invoices(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT, expense_date TEXT NOT NULL,
            supplier TEXT NOT NULL DEFAULT '', description TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'other', document_number TEXT NOT NULL DEFAULT '',
            amount TEXT NOT NULL, currency TEXT NOT NULL DEFAULT 'CZK', czk_rate TEXT NOT NULL DEFAULT '1',
            business_percent TEXT NOT NULL DEFAULT '100', payment_method TEXT NOT NULL DEFAULT 'bank_transfer',
            dph_obligation TEXT NOT NULL DEFAULT 'none', supplier_vat_id TEXT NOT NULL DEFAULT '',
            country TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS dph_filings (
            id INTEGER PRIMARY KEY AUTOINCREMENT, period_year INTEGER NOT NULL, period_month INTEGER NOT NULL,
            filing_kind TEXT NOT NULL DEFAULT 'SHV', status TEXT NOT NULL DEFAULT 'draft',
            filed_date TEXT NOT NULL DEFAULT '', confirmation_number TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(period_year, period_month, filing_kind)
        );
        """
    )

    def columns(table: str) -> set[str]:
        return {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}

    migrations: list[tuple[str, str, str]] = []
    for column, definition in (
        ("tax_first_name", "TEXT NOT NULL DEFAULT 'Jakub Karol'"),
        ("tax_last_name", "TEXT NOT NULL DEFAULT 'Maćkowski'"),
        ("tax_office_code", "TEXT NOT NULL DEFAULT '451'"),
        ("tax_branch_code", "TEXT NOT NULL DEFAULT '2002'"),
    ):
        if column not in columns("company"):
            migrations.append(("company", column, definition))
    for column, definition in (
        ("czk_rate", "TEXT NOT NULL DEFAULT ''"),
        ("dph_category", "TEXT NOT NULL DEFAULT 'auto'"),
    ):
        if column not in columns("invoices"):
            migrations.append(("invoices", column, definition))

    if migrations and DB_PATH.exists() and DB_PATH.stat().st_size:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = BACKUP_DIR / f"przed_aktualizacja_V3_{timestamp}.sqlite3"
        backup_conn = sqlite3.connect(backup_path)
        try:
            db.backup(backup_conn)
        finally:
            backup_conn.close()
    for table, column, definition in migrations:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    existing = db.execute("SELECT id FROM company WHERE id = 1").fetchone()
    if existing is None:
        db.execute(
            """INSERT INTO company (
                id, name, address, postal_code, city, country, company_id, vat_id,
                email, phone, bank_name, bank_account, iban, bic,
                default_currency, default_due_days, invoice_prefix, default_language,
                default_tax_mode, default_reverse_charge_note
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, '', '', '', '', '', '', 'EUR', 14, 'FV', 'de', 'reverse_charge', ?)""",
            ("Jakub Karol Maćkowski", "Uralská 689/7", "160 00", "Praha 6 - Bubeneč",
             "Česká republika", "19506180", "CZ686656725",
             "Steuerschuldnerschaft des Leistungsempfängers (Reverse Charge). / Daň odvede zákazník."),
        )
    db.commit()


def init_tax_module() -> None:
    db = get_db()
    invoice_columns = {row["name"] for row in db.execute("PRAGMA table_info(invoices)").fetchall()}
    tables = {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    tax_columns = {row["name"] for row in db.execute("PRAGMA table_info(tax_settings)").fetchall()} if "tax_settings" in tables else set()
    required_tax_columns = {"employment_tax_withheld", "cssz_main_from_date", "cssz_secondary_min_monthly_base", "vzp_main_from_date"}
    changes_needed = (
        "paid_date" not in invoice_columns
        or "tax_settings" not in tables
        or "tax_payments" not in tables
        or not required_tax_columns.issubset(tax_columns)
    )
    if changes_needed and DB_PATH.exists() and DB_PATH.stat().st_size:
        backup_path = BACKUP_DIR / f"przed_aktualizacja_V5_{datetime.now().strftime('%Y%m%d_%H%M%S')}.sqlite3"
        backup_conn = sqlite3.connect(backup_path)
        try:
            db.backup(backup_conn)
        finally:
            backup_conn.close()
    if "paid_date" not in invoice_columns:
        db.execute("ALTER TABLE invoices ADD COLUMN paid_date TEXT NOT NULL DEFAULT ''")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS tax_settings (
            year INTEGER PRIMARY KEY,
            activity_start_date TEXT NOT NULL DEFAULT '', activity_end_date TEXT NOT NULL DEFAULT '',
            revenue_basis TEXT NOT NULL DEFAULT 'paid',
            expense_percent TEXT NOT NULL DEFAULT '60', expense_limit TEXT NOT NULL DEFAULT '1200000',
            income_tax_credit TEXT NOT NULL DEFAULT '30840', employment_tax_base TEXT NOT NULL DEFAULT '0',
            employment_tax_withheld TEXT NOT NULL DEFAULT '0', tax_threshold TEXT NOT NULL DEFAULT '1762812',
            cssz_mode TEXT NOT NULL DEFAULT 'main', cssz_main_from_date TEXT NOT NULL DEFAULT '',
            cssz_threshold_annual TEXT NOT NULL DEFAULT '117521',
            cssz_threshold_reduction_month TEXT NOT NULL DEFAULT '9794',
            cssz_assessment_percent TEXT NOT NULL DEFAULT '55', cssz_rate_percent TEXT NOT NULL DEFAULT '29.2',
            cssz_min_monthly_base TEXT NOT NULL DEFAULT '17139',
            cssz_secondary_min_monthly_base TEXT NOT NULL DEFAULT '5387',
            cssz_monthly_advance TEXT NOT NULL DEFAULT '5005',
            vzp_mode TEXT NOT NULL DEFAULT 'main', vzp_main_from_date TEXT NOT NULL DEFAULT '',
            vzp_assessment_percent TEXT NOT NULL DEFAULT '50', vzp_rate_percent TEXT NOT NULL DEFAULT '13.5',
            vzp_min_monthly_base TEXT NOT NULL DEFAULT '24483.50', vzp_monthly_advance TEXT NOT NULL DEFAULT '3306',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS tax_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, payment_date TEXT NOT NULL,
            payment_type TEXT NOT NULL, amount TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
    """)
    tax_columns = {row["name"] for row in db.execute("PRAGMA table_info(tax_settings)").fetchall()}
    additions = {
        "employment_tax_withheld": "TEXT NOT NULL DEFAULT '0'",
        "cssz_main_from_date": "TEXT NOT NULL DEFAULT ''",
        "cssz_secondary_min_monthly_base": "TEXT NOT NULL DEFAULT '5387'",
        "vzp_main_from_date": "TEXT NOT NULL DEFAULT ''",
    }
    added_any = False
    for name, definition in additions.items():
        if name not in tax_columns:
            db.execute(f"ALTER TABLE tax_settings ADD COLUMN {name} {definition}")
            added_any = True

    # Jednorazowa migracja ustawień użytkownika: lipiec 2026 vedlejší, od sierpnia hlavní.
    if added_any:
        row_2026 = db.execute("SELECT * FROM tax_settings WHERE year=2026").fetchone()
        if row_2026 is not None:
            current = dict(row_2026)
            updates = {
                "income_tax_credit": "30840" if str(current.get("income_tax_credit", "0")).strip() in {"", "0", "0.0", "0.00"} else current.get("income_tax_credit"),
                "employment_tax_withheld": current.get("employment_tax_withheld", "0") or "0",
                "cssz_main_from_date": "2026-08-01",
                "cssz_secondary_min_monthly_base": "5387",
                "vzp_main_from_date": "2026-08-01",
            }
            if current.get("cssz_mode") != "custom":
                updates.update({"cssz_mode": "mixed", "cssz_min_monthly_base": "17139", "cssz_monthly_advance": "5005"})
            if current.get("vzp_mode") != "custom":
                updates.update({"vzp_mode": "mixed", "vzp_min_monthly_base": "24483.50", "vzp_monthly_advance": "3306"})
            keys = list(updates)
            db.execute("UPDATE tax_settings SET " + ",".join(f"{key}=?" for key in keys) + " WHERE year=2026", tuple(updates[key] for key in keys))
    db.commit()


with app.app_context():
    init_db()
    init_tax_module()
    if REMOTE_DB_ENABLED:
        upload_remote_database()


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def get_company() -> dict[str, Any]:
    row = get_db().execute("SELECT * FROM company WHERE id = 1").fetchone()
    if row is None:
        raise RuntimeError("Brak danych firmy.")
    return dict(row)


def parse_decimal(value: Any, field_name: str = "wartość") -> Decimal:
    raw = str(value or "").strip().replace(" ", "").replace(",", ".")
    if not raw:
        raw = "0"
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"Nieprawidłowa {field_name}: {value}") from exc


def q2(value: Decimal) -> Decimal:
    return value.quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def format_decimal(value: Any, decimals: int = 2) -> str:
    try:
        number = parse_decimal(value)
    except ValueError:
        return str(value)
    formatted = f"{number:,.{decimals}f}"
    return formatted.replace(",", " ")


@app.template_filter("money")
def money_filter(value: Any, currency: str = "") -> str:
    return f"{format_decimal(value)} {currency}".strip()


@app.template_filter("date_pl")
def date_pl_filter(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        return value or ""


def normalize_vat_id(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()


def split_eu_vat_id(value: Any) -> tuple[str, str] | None:
    normalized = normalize_vat_id(value)
    if len(normalized) < 4:
        return None
    prefix = normalized[:2]
    if prefix == "GR":
        prefix = "EL"
    if prefix not in EU_VAT_PREFIXES:
        return None
    number = normalized[2:]
    if not number or len(number) > 12 or not re.fullmatch(r"[A-Z0-9]+", number):
        return None
    return prefix, number


def decimal_rate(currency: Any, rate: Any) -> Decimal | None:
    code = str(currency or "").strip().upper()
    if code == "CZK":
        return Decimal("1")
    try:
        parsed = parse_decimal(rate, "kurs do CZK")
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def month_bounds(year: int, month: int) -> tuple[str, str]:
    return date(year, month, 1).isoformat(), date(year, month, calendar.monthrange(year, month)[1]).isoformat()


def period_label(year: int, month: int) -> str:
    return f"{MONTHS.get(month, str(month))} {year}"


def dph_due_date(year: int, month: int) -> date:
    return date(year + 1, 1, 25) if month == 12 else date(year, month + 1, 25)


def effective_dph_kind(invoice: dict[str, Any]) -> tuple[str, str | None]:
    manual = invoice.get("dph_category", "auto")
    vat_split = split_eu_vat_id(invoice.get("contractor_vat_id", ""))
    if manual == "not_required":
        return "not_required", None
    if manual == "review":
        return "review", "Faktura została oznaczona do ręcznej oceny."
    if manual == "eu_service":
        if invoice.get("tax_mode") != "reverse_charge":
            return "review", "Wybrano SH VIES, ale faktura nie ma trybu reverse charge."
        if vat_split is None or vat_split[0] == "CZ":
            return "review", "Brak poprawnego VAT ID kontrahenta z innego państwa UE."
        return "eu_service", None
    if invoice.get("tax_mode") == "reverse_charge":
        if vat_split is not None and vat_split[0] != "CZ":
            return "eu_service", None
        return "review", "Reverse charge bez rozpoznanego VAT ID z innego państwa UE."
    return "not_required", None


def invoice_value_czk(invoice: dict[str, Any], totals: dict[str, Any]) -> Decimal | None:
    rate = decimal_rate(invoice.get("currency", ""), invoice.get("czk_rate", ""))
    return q2(totals["total_net"] * rate) if rate is not None else None


def invoice_dph_info(invoice: dict[str, Any], totals: dict[str, Any]) -> dict[str, Any]:
    kind, warning = effective_dph_kind(invoice)
    value = invoice_value_czk(invoice, totals)
    try:
        service_date = date.fromisoformat(invoice["supply_date"])
    except (ValueError, TypeError):
        service_date = date.today()
    if kind == "eu_service" and value is None:
        warning = "Uzupełnij kurs waluty do CZK przed przygotowaniem Souhrnné hlášení."
    labels = {"eu_service": "SH VIES - usługa UE, kod 3", "not_required": "Bez SH VIES", "review": "Do sprawdzenia"}
    return {
        "kind": kind, "label": labels[kind], "warning": warning, "value_czk": value,
        "period_label": period_label(service_date.year, service_date.month),
        "due_date": dph_due_date(service_date.year, service_date.month).isoformat(),
    }


def build_dph_month_report(year: int, month: int) -> dict[str, Any]:
    start, end = month_bounds(year, month)
    db = get_db()
    invoice_rows = db.execute(
        """SELECT i.*, c.name AS contractor_name, c.vat_id AS contractor_vat_id, c.country AS contractor_country
           FROM invoices i JOIN contractors c ON c.id=i.contractor_id
           WHERE i.supply_date BETWEEN ? AND ? ORDER BY i.supply_date, i.id""", (start, end)
    ).fetchall()
    report_invoices: list[dict[str, Any]] = []
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    warnings: list[str] = []
    for row in invoice_rows:
        invoice = dict(row)
        items = db.execute("SELECT * FROM invoice_items WHERE invoice_id=?", (invoice["id"],)).fetchall()
        totals = calculate_items([dict(item) for item in items], invoice["tax_mode"])
        kind, warning = effective_dph_kind(invoice)
        if kind == "review":
            warnings.append(f"{invoice['invoice_number']}: {warning}")
            continue
        if kind != "eu_service":
            continue
        vat_split = split_eu_vat_id(invoice.get("contractor_vat_id", ""))
        value_czk = invoice_value_czk(invoice, totals)
        rate = decimal_rate(invoice.get("currency", ""), invoice.get("czk_rate", ""))
        record = dict(invoice)
        record.update({"total_net": totals["total_net"], "value_czk": value_czk, "rate": rate})
        report_invoices.append(record)
        if vat_split is None or vat_split[0] == "CZ":
            warnings.append(f"{invoice['invoice_number']}: nieprawidłowy unijny VAT ID.")
            continue
        if value_czk is None:
            warnings.append(f"{invoice['invoice_number']}: uzupełnij kurs {invoice['currency']} do CZK.")
            continue
        key = vat_split
        group = groups.setdefault(key, {"country_code": key[0], "vat_number": key[1], "count": 0, "exact": Decimal("0")})
        group["count"] += 1
        group["exact"] += value_czk
    rows: list[dict[str, Any]] = []
    for group in sorted(groups.values(), key=lambda item: (item["country_code"], item["vat_number"])):
        rows.append({**group, "value_czk": group["exact"].quantize(Decimal("1"), rounding=ROUND_CEILING)})
    total = sum((row["value_czk"] for row in rows), Decimal("0"))
    return {"invoices": report_invoices, "invoice_count": len(report_invoices), "rows": rows,
            "warnings": warnings, "total_czk": total, "block_export": bool(warnings) or not rows}


def parse_company_address(address: str) -> tuple[str, str, str]:
    value = (address or "").strip()
    match = re.match(r"^(.*?)\s+(\d+)(?:/(\d+[A-Za-z]?))?$", value)
    return (match.group(1).strip(), match.group(2), match.group(3) or "") if match else (value, "", "")


def build_shv_xml(year: int, month: int, report: dict[str, Any], company: dict[str, Any]) -> bytes:
    if report["block_export"]:
        raise ValueError("Raport zawiera błędy albo nie ma wierszy do eksportu.")
    dic = normalize_vat_id(company.get("vat_id", ""))
    if dic.startswith("CZ"):
        dic = dic[2:]
    if not dic or not dic.isdigit():
        raise ValueError("W danych firmy wpisz poprawne czeskie DIČ.")
    first_name = str(company.get("tax_first_name", "")).strip()
    last_name = str(company.get("tax_last_name", "")).strip()
    office = str(company.get("tax_office_code", "")).strip()
    branch = str(company.get("tax_branch_code", "")).strip()
    if not first_name or not last_name:
        raise ValueError("Uzupełnij imię i nazwisko podatnika w danych firmy.")
    if not office.isdigit() or not branch.isdigit():
        raise ValueError("Uzupełnij numery urzędu i placówki w danych firmy.")
    street, c_pop, c_orient = parse_company_address(company.get("address", ""))
    root = ET.Element("Pisemnost", {"nazevSW": "Faktury OSVC", "verzeSW": "4.0"})
    doc = ET.SubElement(root, "DPHSHV", {"verzePis": "02.01.04"})
    ET.SubElement(doc, "VetaD", {"dokument": "SHV", "k_uladis": "DPH", "shvies_forma": "R",
                                      "d_poddp": date.today().strftime("%d.%m.%Y"), "rok": str(year), "mesic": str(month)})
    subject = {"c_ufo": office, "c_pracufo": branch, "dic": dic, "typ_ds": "F",
               "prijmeni": last_name, "jmeno": first_name, "naz_obce": company.get("city", ""),
               "ulice": street, "psc": re.sub(r"\s+", "", company.get("postal_code", "")),
               "stat": "ČESKÁ REPUBLIKA", "sest_prijmeni": last_name, "sest_jmeno": first_name}
    if c_pop:
        subject["c_pop"] = c_pop
    if c_orient:
        subject["c_orient"] = c_orient
    phone = re.sub(r"\s+", "", company.get("phone", ""))
    if phone:
        subject["sest_telef"] = phone[:14]
    ET.SubElement(doc, "VetaP", subject)
    for index, row in enumerate(report["rows"], start=1):
        ET.SubElement(doc, "VetaR", {"por_c_stran": "1", "c_rad": str(index),
            "k_stat": row["country_code"], "c_vat": row["vat_number"], "k_pln_eu": "3",
            "pln_pocet": str(row["count"]), "pln_hodnota": str(int(row["value_czk"]))})
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def expense_value_czk(expense: dict[str, Any]) -> Decimal | None:
    rate = decimal_rate(expense.get("currency", ""), expense.get("czk_rate", ""))
    if rate is None:
        return None
    try:
        return q2(parse_decimal(expense.get("amount"), "kwota") * rate)
    except ValueError:
        return None


def month_revenue_czk(year: int, month: int) -> tuple[Decimal, int]:
    start, end = month_bounds(year, month)
    db = get_db()
    rows = db.execute("SELECT * FROM invoices WHERE supply_date BETWEEN ? AND ?", (start, end)).fetchall()
    total = Decimal("0")
    missing = 0
    for row in rows:
        invoice = dict(row)
        items = db.execute("SELECT * FROM invoice_items WHERE invoice_id=?", (invoice["id"],)).fetchall()
        totals = calculate_items([dict(item) for item in items], invoice["tax_mode"])
        rate = decimal_rate(invoice["currency"], invoice.get("czk_rate", ""))
        if rate is None:
            missing += 1
        else:
            total += q2(totals["total_gross"] * rate)
    return q2(total), missing


def get_pending_dph_reminder() -> dict[str, Any] | None:
    db = get_db()
    periods = db.execute("SELECT DISTINCT substr(supply_date,1,7) AS period FROM invoices ORDER BY period").fetchall()
    for row in periods:
        try:
            year, month = map(int, row["period"].split("-"))
        except (ValueError, AttributeError):
            continue
        report = build_dph_month_report(year, month)
        if not report["invoices"] and not report["warnings"]:
            continue
        filing = db.execute("SELECT status FROM dph_filings WHERE period_year=? AND period_month=? AND filing_kind='SHV'", (year, month)).fetchone()
        if filing is None or filing["status"] != "filed":
            return {"year": year, "month": month, "invoice_count": report["invoice_count"],
                    "period_label": period_label(year, month), "due_date": dph_due_date(year, month).isoformat()}
    return None

def calculate_items(items: list[dict[str, Any]], tax_mode: str) -> dict[str, Any]:
    calculated: list[dict[str, Any]] = []
    total_net = Decimal("0")
    total_vat = Decimal("0")
    total_gross = Decimal("0")

    for item in items:
        quantity = parse_decimal(item.get("quantity"), "ilość")
        unit_price = parse_decimal(item.get("unit_price"), "cena jednostkowa")
        vat_rate = parse_decimal(item.get("vat_rate"), "stawka VAT")
        if tax_mode != "vat":
            vat_rate = Decimal("0")
        net = q2(quantity * unit_price)
        vat = q2(net * vat_rate / Decimal("100"))
        gross = q2(net + vat)
        calculated_item = dict(item)
        calculated_item.update(
            {
                "quantity_decimal": quantity,
                "unit_price_decimal": unit_price,
                "vat_rate_decimal": vat_rate,
                "net": net,
                "vat": vat,
                "gross": gross,
            }
        )
        calculated.append(calculated_item)
        total_net += net
        total_vat += vat
        total_gross += gross

    return {
        "items": calculated,
        "total_net": q2(total_net),
        "total_vat": q2(total_vat),
        "total_gross": q2(total_gross),
    }


def get_invoice_bundle(invoice_id: int) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    db = get_db()
    invoice_row = db.execute(
        """
        SELECT i.*, c.name AS contractor_name, c.address AS contractor_address,
               c.postal_code AS contractor_postal_code, c.city AS contractor_city,
               c.country AS contractor_country, c.company_id AS contractor_company_id,
               c.vat_id AS contractor_vat_id, c.email AS contractor_email,
               c.phone AS contractor_phone
        FROM invoices i
        JOIN contractors c ON c.id = i.contractor_id
        WHERE i.id = ?
        """,
        (invoice_id,),
    ).fetchone()
    if invoice_row is None:
        abort(404)
    items_rows = db.execute(
        "SELECT * FROM invoice_items WHERE invoice_id = ? ORDER BY position, id",
        (invoice_id,),
    ).fetchall()
    invoice = dict(invoice_row)
    items = [dict(row) for row in items_rows]
    totals = calculate_items(items, invoice["tax_mode"])
    return invoice, items, totals


def next_invoice_identity(db: sqlite3.Connection, issue_date: str, prefix: str) -> tuple[str, int, int]:
    try:
        year = date.fromisoformat(issue_date).year
    except ValueError as exc:
        raise ValueError("Nieprawidłowa data wystawienia.") from exc

    sequence_row = db.execute(
        "SELECT next_number FROM invoice_sequences WHERE year = ?", (year,)
    ).fetchone()
    if sequence_row is None:
        sequence = 1
        db.execute(
            "INSERT INTO invoice_sequences (year, next_number) VALUES (?, ?)",
            (year, 2),
        )
    else:
        sequence = int(sequence_row["next_number"])
        db.execute(
            "UPDATE invoice_sequences SET next_number = ? WHERE year = ?",
            (sequence + 1, year),
        )
    clean_prefix = (prefix or "FV").strip() or "FV"
    return f"{clean_prefix}-{year}-{sequence:03d}", year, sequence


def invoice_form_from_request(existing_invoice: dict[str, Any] | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    invoice = {
        "id": existing_invoice.get("id") if existing_invoice else None,
        "invoice_number": existing_invoice.get("invoice_number", "") if existing_invoice else "",
        "contractor_id": request.form.get("contractor_id", ""),
        "issue_date": request.form.get("issue_date", ""), "supply_date": request.form.get("supply_date", ""),
        "due_date": request.form.get("due_date", ""), "currency": request.form.get("currency", "EUR").strip().upper(),
        "language": request.form.get("language", "de"), "tax_mode": request.form.get("tax_mode", "reverse_charge"),
        "payment_method": request.form.get("payment_method", "bank_transfer"),
        "variable_symbol": request.form.get("variable_symbol", "").strip(),
        "order_number": request.form.get("order_number", "").strip(),
        "reverse_charge_note": request.form.get("reverse_charge_note", "").strip(),
        "notes": request.form.get("notes", "").strip(), "czk_rate": request.form.get("czk_rate", "").strip(),
        "dph_category": request.form.get("dph_category", "auto"),
        "status": existing_invoice.get("status", "unpaid") if existing_invoice else "unpaid",
    }
    descriptions = request.form.getlist("item_description"); quantities = request.form.getlist("item_quantity")
    units = request.form.getlist("item_unit"); unit_prices = request.form.getlist("item_unit_price")
    vat_rates = request.form.getlist("item_vat_rate")
    count = max(len(descriptions), len(quantities), len(units), len(unit_prices), len(vat_rates), 1)
    items: list[dict[str, Any]] = []
    for index in range(count):
        description = descriptions[index].strip() if index < len(descriptions) else ""
        quantity = quantities[index].strip() if index < len(quantities) else "1"
        unit = units[index].strip() if index < len(units) else "h"
        price = unit_prices[index].strip() if index < len(unit_prices) else "0"
        vat = vat_rates[index].strip() if index < len(vat_rates) else "0"
        if description or price:
            items.append({"description": description, "quantity": quantity or "1", "unit": unit or "szt.",
                          "unit_price": price or "0", "vat_rate": vat or "0"})
    if not items:
        items = [{"description": "", "quantity": "1", "unit": "h", "unit_price": "", "vat_rate": "0"}]
    return invoice, items


def validate_invoice(invoice: dict[str, Any], items: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    db = get_db()
    try:
        contractor_id = int(invoice["contractor_id"])
    except (TypeError, ValueError):
        errors.append("Wybierz kontrahenta.")
    else:
        if db.execute("SELECT 1 FROM contractors WHERE id=?", (contractor_id,)).fetchone() is None:
            errors.append("Wybrany kontrahent nie istnieje.")
    for key, label in (("issue_date", "data wystawienia"), ("supply_date", "data wykonania usługi"), ("due_date", "termin płatności")):
        try:
            date.fromisoformat(invoice.get(key, ""))
        except ValueError:
            errors.append(f"Uzupełnij poprawnie pole: {label}.")
    if not invoice.get("currency") or len(invoice["currency"]) > 6:
        errors.append("Podaj poprawny kod waluty.")
    if invoice.get("language") not in LANGUAGES:
        errors.append("Wybierz język faktury.")
    if invoice.get("tax_mode") not in TAX_MODES:
        errors.append("Wybierz sposób rozliczenia VAT.")
    if invoice.get("payment_method") not in PAYMENT_METHODS:
        errors.append("Wybierz sposób płatności.")
    if invoice.get("dph_category") not in DPH_CATEGORIES:
        errors.append("Wybierz klasyfikację DPH / VIES.")
    if invoice.get("currency") == "CZK":
        invoice["czk_rate"] = "1"
    elif invoice.get("czk_rate"):
        try:
            if parse_decimal(invoice["czk_rate"], "kurs") <= 0:
                raise ValueError
        except ValueError:
            errors.append("Kurs do CZK musi być większy od zera.")
    valid = False
    for index, item in enumerate(items, start=1):
        if not item.get("description", "").strip():
            errors.append(f"Pozycja {index}: wpisz opis.")
            continue
        try:
            quantity = parse_decimal(item.get("quantity"), "ilość"); price = parse_decimal(item.get("unit_price"), "cena")
            vat = parse_decimal(item.get("vat_rate"), "stawka VAT")
        except ValueError as exc:
            errors.append(f"Pozycja {index}: {exc}"); continue
        if quantity <= 0: errors.append(f"Pozycja {index}: ilość musi być większa od zera.")
        if price < 0: errors.append(f"Pozycja {index}: cena nie może być ujemna.")
        if vat < 0 or vat > 100: errors.append(f"Pozycja {index}: stawka VAT musi być 0-100.")
        valid = True
    if not valid:
        errors.append("Dodaj przynajmniej jedną prawidłową pozycję.")
    return errors


@app.context_processor
def inject_globals() -> dict[str, Any]:
    return {"LANGUAGES": LANGUAGES, "TAX_MODES": TAX_MODES, "PAYMENT_METHODS": PAYMENT_METHODS,
            "DPH_CATEGORIES": DPH_CATEGORIES, "EXPENSE_CATEGORIES": EXPENSE_CATEGORIES,
            "EXPENSE_DPH_OBLIGATIONS": EXPENSE_DPH_OBLIGATIONS, "MONTHS": MONTHS,
            "TAX_PAYMENT_TYPES": TAX_PAYMENT_TYPES}



@app.context_processor
def inject_v7_globals() -> dict[str, Any]:
    return {"auth_enabled": bool(APP_PASSWORD), "cloud_mode": CLOUD_MODE}



@app.before_request
def v72_track_database_state():
    try:
        g.db_mtime_before = DB_PATH.stat().st_mtime_ns if DB_PATH.exists() else 0
        g.db_size_before = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    except OSError:
        g.db_mtime_before = 0
        g.db_size_before = 0


@app.after_request
def v72_sync_database_after_change(response):
    if not REMOTE_DB_ENABLED or response.status_code >= 500:
        return response
    try:
        current_mtime = DB_PATH.stat().st_mtime_ns if DB_PATH.exists() else 0
        current_size = DB_PATH.stat().st_size if DB_PATH.exists() else 0
        changed = (
            current_mtime != getattr(g, "db_mtime_before", current_mtime)
            or current_size != getattr(g, "db_size_before", current_size)
        )
        if changed:
            upload_remote_database()
    except Exception as exc:
        print(f"Supabase: błąd automatycznej synchronizacji: {exc}")
    return response


@app.before_request
def v7_auth_guard():
    # PWA assets i logowanie muszą działać przed zalogowaniem.
    allowed = {"login", "manifest", "service_worker", "app_icon", "static", "health"}
    if not APP_PASSWORD or request.endpoint in allowed:
        return None
    if session.get("authenticated") is True:
        return None
    return redirect(url_for("login", next=request.full_path if request.query_string else request.path))


@app.route("/login", methods=["GET", "POST"])
def login():
    if not APP_PASSWORD:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        supplied = request.form.get("password", "")
        if hmac.compare_digest(supplied, APP_PASSWORD):
            session["authenticated"] = True
            session.permanent = True
            return redirect(request.args.get("next") or url_for("dashboard"))
        flash("Nieprawidłowe hasło.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/manifest.webmanifest")
def manifest():
    payload = """{
      "name":"Faktury OSVČ",
      "short_name":"Faktury",
      "start_url":"/",
      "display":"standalone",
      "background_color":"#f4f6f8",
      "theme_color":"#8b1538",
      "icons":[{"src":"/app-icon.svg","sizes":"any","type":"image/svg+xml","purpose":"any maskable"}]
    }"""
    return Response(payload, mimetype="application/manifest+json")


@app.route("/service-worker.js")
def service_worker():
    # Nie cache'ujemy danych finansowych offline. SW daje instalowalność PWA
    # i prosty fallback dla powłoki aplikacji.
    script = """const CACHE='faktury-shell-v7';
self.addEventListener('install',e=>{self.skipWaiting();});
self.addEventListener('activate',e=>{e.waitUntil(self.clients.claim());});
self.addEventListener('fetch',e=>{
  if(e.request.method!=='GET') return;
  e.respondWith(fetch(e.request).catch(()=>caches.match(e.request)));
});"""
    return Response(script, mimetype="application/javascript")


@app.route("/app-icon.svg")
def app_icon():
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
    <rect width="512" height="512" rx="112" fill="#8b1538"/>
    <text x="256" y="310" text-anchor="middle" font-family="Arial,sans-serif" font-size="220" font-weight="700" fill="white">F</text>
    </svg>"""
    return Response(svg, mimetype="image/svg+xml")


@app.route("/health")
def health():
    return {
        "status": "ok",
        "cloud_mode": CLOUD_MODE,
        "supabase_configured": REMOTE_DB_ENABLED,
        "supabase_bucket": SUPABASE_BUCKET if REMOTE_DB_ENABLED else None,
        "supabase_key_kind": SUPABASE_KEY_KIND,
        "database_exists": DB_PATH.exists(),
    }



@app.route("/")
def dashboard() -> str:
    db = get_db(); today = date.today()
    counts = {"contractors": db.execute("SELECT COUNT(*) AS c FROM contractors").fetchone()["c"],
              "invoices": db.execute("SELECT COUNT(*) AS c FROM invoices").fetchone()["c"],
              "unpaid": db.execute("SELECT COUNT(*) AS c FROM invoices WHERE status='unpaid'").fetchone()["c"]}
    rows = db.execute("""SELECT i.*, c.name AS contractor_name FROM invoices i JOIN contractors c ON c.id=i.contractor_id
                       ORDER BY i.issue_date DESC, i.id DESC LIMIT 8""").fetchall()
    recent = []
    for row in rows:
        invoice = dict(row); items = db.execute("SELECT * FROM invoice_items WHERE invoice_id=?", (invoice["id"],)).fetchall()
        invoice["total"] = calculate_items([dict(item) for item in items], invoice["tax_mode"])["total_gross"]
        recent.append(invoice)
    return render_template("dashboard.html", counts=counts, recent=recent, company=get_company(),
                           tax_snapshot=compute_tax_snapshot(today.year), dph_reminder=get_pending_dph_reminder())


@app.route("/company", methods=["GET", "POST"])
def company_edit() -> str:
    db = get_db(); company = get_company()
    if request.method == "POST":
        data = {key: request.form.get(key, "").strip() for key in (
            "name", "address", "postal_code", "city", "country", "company_id", "email", "phone",
            "bank_name", "bank_account", "invoice_prefix", "default_reverse_charge_note",
            "tax_first_name", "tax_last_name", "tax_office_code", "tax_branch_code")}
        data["vat_id"] = request.form.get("vat_id", "").strip().upper()
        data["iban"] = request.form.get("iban", "").replace(" ", "").strip().upper()
        data["bic"] = request.form.get("bic", "").replace(" ", "").strip().upper()
        data["default_currency"] = request.form.get("default_currency", "EUR").strip().upper()
        data["default_due_days"] = request.form.get("default_due_days", "14").strip()
        data["default_language"] = request.form.get("default_language", "de")
        data["default_tax_mode"] = request.form.get("default_tax_mode", "reverse_charge")
        errors = []
        if not data["name"]: errors.append("Nazwa firmy / imię i nazwisko jest wymagane.")
        try:
            due = int(data["default_due_days"])
            if due < 0 or due > 365: raise ValueError
        except ValueError:
            due = 14; errors.append("Termin płatności musi mieć 0-365 dni.")
        if data["default_language"] not in LANGUAGES: errors.append("Wybierz poprawny język.")
        if data["default_tax_mode"] not in TAX_MODES: errors.append("Wybierz poprawny tryb VAT.")
        if data["tax_office_code"] and not data["tax_office_code"].isdigit(): errors.append("Kod c_ufo musi być liczbą.")
        if data["tax_branch_code"] and not data["tax_branch_code"].isdigit(): errors.append("Kod c_pracufo musi być liczbą.")
        if errors:
            for error in errors: flash(error, "error")
            company.update(data); company["default_due_days"] = due
            return render_template("company_form.html", company=company)
        db.execute("""UPDATE company SET name=?, address=?, postal_code=?, city=?, country=?, company_id=?, vat_id=?,
            email=?, phone=?, bank_name=?, bank_account=?, iban=?, bic=?, default_currency=?, default_due_days=?,
            invoice_prefix=?, default_language=?, default_tax_mode=?, default_reverse_charge_note=?, tax_first_name=?,
            tax_last_name=?, tax_office_code=?, tax_branch_code=? WHERE id=1""",
            (data["name"], data["address"], data["postal_code"], data["city"], data["country"], data["company_id"],
             data["vat_id"], data["email"], data["phone"], data["bank_name"], data["bank_account"], data["iban"],
             data["bic"], data["default_currency"], due, data["invoice_prefix"], data["default_language"],
             data["default_tax_mode"], data["default_reverse_charge_note"], data["tax_first_name"],
             data["tax_last_name"], data["tax_office_code"], data["tax_branch_code"]))
        db.commit(); flash("Dane firmy zostały zapisane.", "success")
        return redirect(url_for("company_edit"))
    return render_template("company_form.html", company=company)



@app.route("/projects")
def projects_list() -> str:
    rows = get_db().execute(
        """SELECT p.*, c.name AS contractor_name
           FROM projects p
           LEFT JOIN contractors c ON c.id=p.contractor_id
           ORDER BY p.active DESC, p.project_number COLLATE NOCASE"""
    ).fetchall()
    return render_template("projects_list.html", projects=[dict(r) for r in rows])


def _project_payload(existing_id: int | None = None) -> dict[str, Any]:
    contractor_raw = request.form.get("contractor_id", "").strip()
    return {
        "id": existing_id,
        "project_number": request.form.get("project_number", "").strip(),
        "project_name": request.form.get("project_name", "").strip(),
        "contractor_id": int(contractor_raw) if contractor_raw.isdigit() else None,
        "notes": request.form.get("notes", "").strip(),
        "active": 1 if request.form.get("active") == "1" else 0,
    }


@app.route("/projects/new", methods=["GET", "POST"])
def project_new() -> str:
    db = get_db()
    contractors = [dict(r) for r in db.execute("SELECT * FROM contractors ORDER BY name COLLATE NOCASE").fetchall()]
    project = {"id": None, "project_number": "", "project_name": "", "contractor_id": "", "notes": "", "active": 1}
    if request.method == "POST":
        project = _project_payload()
        if not project["project_number"]:
            flash("Podaj numer projektu.", "error")
            return render_template("project_form.html", project=project, contractors=contractors, is_edit=False)
        try:
            db.execute(
                """INSERT INTO projects (contractor_id, project_number, project_name, notes, active)
                   VALUES (?, ?, ?, ?, ?)""",
                (project["contractor_id"], project["project_number"], project["project_name"], project["notes"], project["active"]),
            )
            db.commit()
        except sqlite3.IntegrityError:
            flash("Taki numer projektu jest już zapisany dla tego kontrahenta.", "error")
            return render_template("project_form.html", project=project, contractors=contractors, is_edit=False)
        flash("Projekt został zapisany.", "success")
        return redirect(url_for("projects_list"))
    return render_template("project_form.html", project=project, contractors=contractors, is_edit=False)


@app.route("/projects/<int:project_id>/edit", methods=["GET", "POST"])
def project_edit(project_id: int) -> str:
    db = get_db()
    row = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if row is None:
        abort(404)
    contractors = [dict(r) for r in db.execute("SELECT * FROM contractors ORDER BY name COLLATE NOCASE").fetchall()]
    project = dict(row)
    if request.method == "POST":
        project = _project_payload(project_id)
        try:
            db.execute(
                """UPDATE projects SET contractor_id=?, project_number=?, project_name=?, notes=?, active=?,
                   updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (project["contractor_id"], project["project_number"], project["project_name"], project["notes"], project["active"], project_id),
            )
            db.commit()
        except sqlite3.IntegrityError:
            flash("Taki numer projektu jest już zapisany dla tego kontrahenta.", "error")
            return render_template("project_form.html", project=project, contractors=contractors, is_edit=True)
        flash("Projekt został zaktualizowany.", "success")
        return redirect(url_for("projects_list"))
    return render_template("project_form.html", project=project, contractors=contractors, is_edit=True)


@app.post("/projects/<int:project_id>/delete")
def project_delete(project_id: int):
    db = get_db()
    db.execute("DELETE FROM projects WHERE id=?", (project_id,))
    db.commit()
    flash("Projekt został usunięty. Numery zapisane na starych fakturach pozostają bez zmian.", "success")
    return redirect(url_for("projects_list"))



@app.route("/contractors")
def contractors_list() -> str:
    q = request.args.get("q", "").strip()
    db = get_db()
    if q:
        like = f"%{q}%"
        rows = db.execute(
            """
            SELECT * FROM contractors
            WHERE name LIKE ? OR vat_id LIKE ? OR company_id LIKE ? OR city LIKE ?
            ORDER BY name COLLATE NOCASE
            """,
            (like, like, like, like),
        ).fetchall()
    else:
        rows = db.execute("SELECT * FROM contractors ORDER BY name COLLATE NOCASE").fetchall()
    return render_template("contractors_list.html", contractors=[dict(row) for row in rows], q=q)


def contractor_from_request(existing_id: int | None = None) -> dict[str, Any]:
    return {
        "id": existing_id,
        "name": request.form.get("name", "").strip(),
        "address": request.form.get("address", "").strip(),
        "postal_code": request.form.get("postal_code", "").strip(),
        "city": request.form.get("city", "").strip(),
        "country": request.form.get("country", "").strip(),
        "company_id": request.form.get("company_id", "").strip(),
        "vat_id": request.form.get("vat_id", "").strip().upper().replace(" ", ""),
        "email": request.form.get("email", "").strip(),
        "phone": request.form.get("phone", "").strip(),
        "notes": request.form.get("notes", "").strip(),
    }


@app.route("/contractors/new", methods=["GET", "POST"])
def contractor_new() -> str:
    contractor: dict[str, Any] = {
        "name": "", "address": "", "postal_code": "", "city": "", "country": "Österreich",
        "company_id": "", "vat_id": "ATU", "email": "", "phone": "", "notes": "",
    }
    if request.method == "POST":
        contractor = contractor_from_request()
        if not contractor["name"]:
            flash("Nazwa kontrahenta jest wymagana.", "error")
            return render_template("contractor_form.html", contractor=contractor, is_edit=False)
        db = get_db()
        db.execute(
            """
            INSERT INTO contractors (name, address, postal_code, city, country, company_id, vat_id, email, phone, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            tuple(contractor[key] for key in ("name", "address", "postal_code", "city", "country", "company_id", "vat_id", "email", "phone", "notes")),
        )
        db.commit()
        flash("Kontrahent został dodany.", "success")
        return redirect(url_for("contractors_list"))
    return render_template("contractor_form.html", contractor=contractor, is_edit=False)


@app.route("/contractors/<int:contractor_id>/edit", methods=["GET", "POST"])
def contractor_edit(contractor_id: int) -> str:
    db = get_db()
    row = db.execute("SELECT * FROM contractors WHERE id = ?", (contractor_id,)).fetchone()
    if row is None:
        abort(404)
    contractor = dict(row)
    if request.method == "POST":
        contractor = contractor_from_request(contractor_id)
        if not contractor["name"]:
            flash("Nazwa kontrahenta jest wymagana.", "error")
            return render_template("contractor_form.html", contractor=contractor, is_edit=True)
        db.execute(
            """
            UPDATE contractors SET name = ?, address = ?, postal_code = ?, city = ?, country = ?,
                company_id = ?, vat_id = ?, email = ?, phone = ?, notes = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            tuple(contractor[key] for key in ("name", "address", "postal_code", "city", "country", "company_id", "vat_id", "email", "phone", "notes")) + (contractor_id,),
        )
        db.commit()
        flash("Dane kontrahenta zostały zapisane.", "success")
        return redirect(url_for("contractors_list"))
    return render_template("contractor_form.html", contractor=contractor, is_edit=True)


@app.post("/contractors/<int:contractor_id>/delete")
def contractor_delete(contractor_id: int):
    db = get_db()
    try:
        cursor = db.execute("DELETE FROM contractors WHERE id = ?", (contractor_id,))
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        flash("Nie można usunąć kontrahenta użytego na fakturze.", "error")
    else:
        if cursor.rowcount:
            flash("Kontrahent został usunięty.", "success")
    return redirect(url_for("contractors_list"))


def default_expense() -> dict[str, Any]:
    return {"id": None, "expense_date": date.today().isoformat(), "supplier": "", "description": "",
            "category": "other", "document_number": "", "amount": "", "currency": "CZK", "czk_rate": "1",
            "business_percent": "100", "payment_method": "bank_transfer", "dph_obligation": "none",
            "supplier_vat_id": "", "country": "", "notes": ""}


def expense_from_request(expense_id: int | None = None) -> dict[str, Any]:
    currency = request.form.get("currency", "CZK").strip().upper()
    rate = request.form.get("czk_rate", "").strip() or ("1" if currency == "CZK" else "")
    return {"id": expense_id, "expense_date": request.form.get("expense_date", "").strip(),
            "supplier": request.form.get("supplier", "").strip(), "description": request.form.get("description", "").strip(),
            "category": request.form.get("category", "other"), "document_number": request.form.get("document_number", "").strip(),
            "amount": request.form.get("amount", "").strip(), "currency": currency, "czk_rate": rate,
            "business_percent": request.form.get("business_percent", "100").strip(),
            "payment_method": request.form.get("payment_method", "bank_transfer"),
            "dph_obligation": request.form.get("dph_obligation", "none"),
            "supplier_vat_id": normalize_vat_id(request.form.get("supplier_vat_id", "")),
            "country": request.form.get("country", "").strip(), "notes": request.form.get("notes", "").strip()}


def validate_expense(expense: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    try: date.fromisoformat(expense["expense_date"])
    except ValueError: errors.append("Podaj poprawną datę kosztu.")
    if not expense["description"]: errors.append("Wpisz opis kosztu.")
    try:
        if parse_decimal(expense["amount"]) < 0: raise ValueError
    except ValueError: errors.append("Kwota kosztu musi być liczbą nieujemną.")
    try:
        percent = parse_decimal(expense["business_percent"])
        if percent < 0 or percent > 100: raise ValueError
    except ValueError: errors.append("Część związana z działalnością musi wynosić 0-100%.")
    if expense["currency"] != "CZK":
        try:
            if parse_decimal(expense["czk_rate"]) <= 0: raise ValueError
        except ValueError: errors.append("Dla waluty obcej podaj kurs do CZK.")
    if expense["category"] not in EXPENSE_CATEGORIES: errors.append("Wybierz kategorię kosztu.")
    if expense["payment_method"] not in PAYMENT_METHODS: errors.append("Wybierz sposób płatności.")
    if expense["dph_obligation"] not in EXPENSE_DPH_OBLIGATIONS: errors.append("Wybierz klasyfikację DPH.")
    return errors


@app.route("/expenses")
def expenses_page() -> str:
    today = date.today()
    try:
        year = int(request.args.get("year", today.year)); month = int(request.args.get("month", today.month))
        if month not in range(1, 13): raise ValueError
    except ValueError:
        year, month = today.year, today.month
    start, end = month_bounds(year, month); db = get_db()
    rows = db.execute("SELECT * FROM expenses WHERE expense_date BETWEEN ? AND ? ORDER BY expense_date DESC,id DESC", (start, end)).fetchall()
    expenses = []; actual = Decimal("0"); deductible = Decimal("0"); missing = 0; foreign_count = 0
    category_map: dict[str, Decimal] = {}
    for row in rows:
        expense = dict(row); value = expense_value_czk(expense); expense["amount_czk"] = value; expenses.append(expense)
        if value is None: missing += 1; value = Decimal("0")
        actual += value
        try: deductible += value * parse_decimal(expense["business_percent"]) / Decimal("100")
        except ValueError: pass
        category_map[expense["category"]] = category_map.get(expense["category"], Decimal("0")) + value
        if expense["dph_obligation"] in {"foreign_service", "review"}: foreign_count += 1
    revenue, missing_invoices = month_revenue_czk(year, month)
    category_totals = [{"category": key, "total": q2(value)} for key, value in sorted(category_map.items(), key=lambda p: p[1], reverse=True)]
    totals = {"actual": q2(actual), "deductible": q2(deductible), "revenue": revenue, "result": q2(revenue - actual)}
    return render_template("expenses.html", expenses=expenses, totals=totals, category_totals=category_totals,
        selected_year=year, selected_month=month, period_label=period_label(year, month),
        missing_rates=missing + missing_invoices, foreign_dph_count=foreign_count)


@app.route("/expenses/new", methods=["GET", "POST"])
def expense_new() -> str:
    expense = default_expense()
    if request.method == "POST":
        expense = expense_from_request(); errors = validate_expense(expense)
        if errors:
            for error in errors: flash(error, "error")
            return render_template("expense_form.html", expense=expense, is_edit=False)
        db = get_db(); db.execute("""INSERT INTO expenses (expense_date,supplier,description,category,document_number,
            amount,currency,czk_rate,business_percent,payment_method,dph_obligation,supplier_vat_id,country,notes)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", tuple(expense[key] for key in ("expense_date","supplier","description","category","document_number","amount","currency","czk_rate","business_percent","payment_method","dph_obligation","supplier_vat_id","country","notes")))
        db.commit(); flash("Koszt został zapisany.", "success")
        return redirect(url_for("expenses_page", year=expense["expense_date"][:4], month=int(expense["expense_date"][5:7])))
    return render_template("expense_form.html", expense=expense, is_edit=False)


@app.route("/expenses/<int:expense_id>/edit", methods=["GET", "POST"])
def expense_edit(expense_id: int) -> str:
    db = get_db(); row = db.execute("SELECT * FROM expenses WHERE id=?", (expense_id,)).fetchone()
    if row is None: abort(404)
    expense = dict(row)
    if request.method == "POST":
        expense = expense_from_request(expense_id); errors = validate_expense(expense)
        if errors:
            for error in errors: flash(error, "error")
            return render_template("expense_form.html", expense=expense, is_edit=True)
        db.execute("""UPDATE expenses SET expense_date=?,supplier=?,description=?,category=?,document_number=?,amount=?,
            currency=?,czk_rate=?,business_percent=?,payment_method=?,dph_obligation=?,supplier_vat_id=?,country=?,notes=?,
            updated_at=CURRENT_TIMESTAMP WHERE id=?""", tuple(expense[key] for key in ("expense_date","supplier","description","category","document_number","amount","currency","czk_rate","business_percent","payment_method","dph_obligation","supplier_vat_id","country","notes")) + (expense_id,))
        db.commit(); flash("Koszt został zaktualizowany.", "success")
        return redirect(url_for("expenses_page", year=expense["expense_date"][:4], month=int(expense["expense_date"][5:7])))
    return render_template("expense_form.html", expense=expense, is_edit=True)


@app.post("/expenses/<int:expense_id>/delete")
def expense_delete(expense_id: int):
    db = get_db(); db.execute("DELETE FROM expenses WHERE id=?", (expense_id,)); db.commit()
    flash("Koszt został usunięty.", "success"); return redirect(url_for("expenses_page"))


@app.route("/expenses/export.csv")
def expenses_export_csv():
    today = date.today()
    try: year = int(request.args.get("year", today.year)); month = int(request.args.get("month", today.month))
    except ValueError: year, month = today.year, today.month
    start, end = month_bounds(year, month)
    rows = get_db().execute("SELECT * FROM expenses WHERE expense_date BETWEEN ? AND ? ORDER BY expense_date,id", (start, end)).fetchall()
    output = StringIO(); writer = csv.writer(output, delimiter=';')
    writer.writerow(["Data","Dostawca","Opis","Kategoria","Dokument","Kwota","Waluta","Kurs do CZK","Wartość CZK","Część firmowa %","DPH"])
    for row in rows:
        item = dict(row); value = expense_value_czk(item)
        writer.writerow([item["expense_date"], item["supplier"], item["description"], EXPENSE_CATEGORIES.get(item["category"], item["category"]), item["document_number"], item["amount"], item["currency"], item["czk_rate"], str(value or ""), item["business_percent"], item["dph_obligation"]])
    data = '\ufeff' + output.getvalue()
    return Response(data, mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename=koszty_{year}_{month:02d}.csv"})



def ensure_tax_settings(year: int) -> dict[str, Any]:
    db = get_db()
    row = db.execute("SELECT * FROM tax_settings WHERE year=?", (year,)).fetchone()
    if row is None:
        if year == 2026:
            db.execute("""INSERT INTO tax_settings (
                year, activity_start_date, revenue_basis, expense_percent, expense_limit,
                income_tax_credit, employment_tax_base, employment_tax_withheld, tax_threshold,
                cssz_mode, cssz_main_from_date, cssz_threshold_annual, cssz_threshold_reduction_month,
                cssz_assessment_percent, cssz_rate_percent, cssz_min_monthly_base,
                cssz_secondary_min_monthly_base, cssz_monthly_advance,
                vzp_mode, vzp_main_from_date, vzp_assessment_percent, vzp_rate_percent,
                vzp_min_monthly_base, vzp_monthly_advance
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                2026, "2026-07-08", "paid", "60", "1200000", "30840", "0", "0", "1762812",
                "mixed", "2026-08-01", "117521", "9794", "55", "29.2", "17139", "5387", "5005",
                "mixed", "2026-08-01", "50", "13.5", "24483.50", "3306"
            ))
        else:
            start = f"{year}-01-01"
            db.execute("""INSERT INTO tax_settings (
                year, activity_start_date, income_tax_credit, cssz_mode, cssz_main_from_date,
                cssz_min_monthly_base, cssz_secondary_min_monthly_base, cssz_monthly_advance,
                vzp_mode, vzp_main_from_date, vzp_monthly_advance
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (
                year, start, "30840", "main", start, "17139", "5387", "5005", "main", start, "3306"
            ))
        db.commit()
        row = db.execute("SELECT * FROM tax_settings WHERE year=?", (year,)).fetchone()
    return dict(row)


def _valid_date_or_blank(value: str, label: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Nieprawidłowa data: {label}.") from exc
    return value


def active_month_starts(settings: dict[str, Any], year: int) -> list[date]:
    year_start, year_end = date(year, 1, 1), date(year, 12, 31)
    try:
        start = date.fromisoformat(settings.get("activity_start_date") or year_start.isoformat())
    except ValueError:
        start = year_start
    try:
        end = date.fromisoformat(settings.get("activity_end_date") or year_end.isoformat())
    except ValueError:
        end = year_end
    start, end = max(start, year_start), min(end, year_end)
    if end < start:
        return []
    cursor = date(start.year, start.month, 1)
    last = date(end.year, end.month, 1)
    result: list[date] = []
    while cursor <= last:
        result.append(cursor)
        cursor = date(cursor.year + 1, 1, 1) if cursor.month == 12 else date(cursor.year, cursor.month + 1, 1)
    return result


def active_months_for_year(settings: dict[str, Any], year: int) -> int:
    return len(active_month_starts(settings, year))


def status_month_counts(settings: dict[str, Any], year: int, mode_key: str, main_from_key: str) -> tuple[int, int]:
    months = active_month_starts(settings, year)
    mode = settings.get(mode_key, "main")
    if mode == "main":
        return len(months), 0
    if mode in {"secondary", "custom"}:
        return 0, len(months)
    try:
        transition = date.fromisoformat(settings.get(main_from_key) or f"{year}-01-01")
        transition_month = date(transition.year, transition.month, 1)
    except ValueError:
        transition_month = date(year, 1, 1)
    main = sum(1 for month in months if month >= transition_month)
    return main, len(months) - main


def ceil_whole(value: Decimal) -> Decimal:
    return value.quantize(Decimal("1"), rounding=ROUND_CEILING)


def progressive_tax(base: Decimal, threshold: Decimal) -> Decimal:
    base = max(base, Decimal("0")); threshold = max(threshold, Decimal("0"))
    lower = min(base, threshold)
    higher = max(base - threshold, Decimal("0"))
    return q2(lower * Decimal("0.15") + higher * Decimal("0.23"))


def tax_invoice_rows(year: int, basis: str) -> tuple[list[dict[str, Any]], list[str]]:
    db = get_db()
    rows = db.execute("""SELECT i.*, c.name AS contractor_name, c.vat_id AS contractor_vat_id
                         FROM invoices i JOIN contractors c ON c.id=i.contractor_id
                         ORDER BY i.issue_date, i.id""").fetchall()
    result: list[dict[str, Any]] = []
    warnings: list[str] = []
    for raw in rows:
        invoice = dict(raw)
        if basis == "paid":
            if invoice.get("status") != "paid":
                continue
            recognized = invoice.get("paid_date", "")
            if not recognized:
                recognized = invoice.get("supply_date", "")
                warnings.append(f"{invoice['invoice_number']}: brak daty zapłaty; użyto daty wykonania usługi.")
        else:
            recognized = invoice.get("supply_date", "")
        try:
            recognized_date = date.fromisoformat(recognized)
        except (ValueError, TypeError):
            warnings.append(f"{invoice['invoice_number']}: nieprawidłowa data przychodu.")
            continue
        if recognized_date.year != year:
            continue
        items = db.execute("SELECT * FROM invoice_items WHERE invoice_id=?", (invoice["id"],)).fetchall()
        totals = calculate_items([dict(item) for item in items], invoice["tax_mode"])
        rate = decimal_rate(invoice.get("currency"), invoice.get("czk_rate"))
        if rate is None:
            warnings.append(f"{invoice['invoice_number']}: brak kursu {invoice.get('currency','')} do CZK; faktura nie została doliczona.")
            continue
        taxable_amount = totals["total_net"] if invoice.get("tax_mode") == "vat" else totals["total_gross"]
        invoice["recognized_date"] = recognized_date.isoformat()
        invoice["value_czk"] = q2(taxable_amount * rate)
        result.append(invoice)
    return result, warnings


def compute_tax_snapshot(year: int) -> dict[str, Any]:
    settings = ensure_tax_settings(year)
    warnings: list[str] = []
    basis = settings.get("revenue_basis", "paid")
    invoice_rows, invoice_warnings = tax_invoice_rows(year, basis)
    warnings.extend(invoice_warnings)
    revenue = q2(sum((row["value_czk"] for row in invoice_rows), Decimal("0")))

    def d(key: str, default: str = "0") -> Decimal:
        try:
            return parse_decimal(settings.get(key, default), key)
        except ValueError:
            warnings.append(f"Nieprawidłowe ustawienie {key}; użyto {default}.")
            return Decimal(default)

    expense_percent = min(max(d("expense_percent", "60"), Decimal("0")), Decimal("100"))
    expense_limit = max(d("expense_limit", "1200000"), Decimal("0"))
    flat_expenses = q2(min(revenue * expense_percent / Decimal("100"), expense_limit))
    profit = q2(max(revenue - flat_expenses, Decimal("0")))

    employment_base = max(d("employment_tax_base"), Decimal("0"))
    employment_tax_withheld = max(d("employment_tax_withheld"), Decimal("0"))
    tax_threshold = max(d("tax_threshold", "1762812"), Decimal("0"))
    annual_credit = max(d("income_tax_credit", "30840"), Decimal("0"))
    annual_tax_before_credit = progressive_tax(employment_base + profit, tax_threshold)
    annual_tax_after_credit = max(annual_tax_before_credit - annual_credit, Decimal("0"))
    income_tax = q2(max(annual_tax_after_credit - employment_tax_withheld, Decimal("0")))
    if year == 2026 and employment_base == 0:
        warnings.append("W 2026 byłeś zatrudniony w Accenture do 31.07. Wpisz podstawę podatku i pobrane zaliczki z dokumentu „Potvrzení o zdanitelných příjmech”, inaczej podatek będzie tylko przybliżeniem.")

    active_months = active_months_for_year(settings, year)
    cssz_mode = settings.get("cssz_mode", "main")
    cssz_main_months, cssz_secondary_months = status_month_counts(settings, year, "cssz_mode", "cssz_main_from_date")
    cssz_assessment_percent = max(d("cssz_assessment_percent", "55"), Decimal("0"))
    cssz_rate = max(d("cssz_rate_percent", "29.2"), Decimal("0"))
    cssz_main_min_month = max(d("cssz_min_monthly_base", "17139"), Decimal("0"))
    cssz_secondary_min_month = max(d("cssz_secondary_min_monthly_base", "5387"), Decimal("0"))
    threshold_annual = d("cssz_threshold_annual", "117521")
    threshold_reduction = d("cssz_threshold_reduction_month", "9794")
    cssz_threshold = max(threshold_annual - threshold_reduction * Decimal(12 - cssz_secondary_months), Decimal("0")) if cssz_secondary_months else Decimal("0")
    average_profit = profit / Decimal(active_months) if active_months else Decimal("0")
    main_profit = average_profit * Decimal(cssz_main_months)
    secondary_profit = average_profit * Decimal(cssz_secondary_months)
    calc_main_assessment = ceil_whole(main_profit * cssz_assessment_percent / Decimal("100"))
    calc_secondary_assessment = ceil_whole(secondary_profit * cssz_assessment_percent / Decimal("100"))
    min_main_assessment = ceil_whole(cssz_main_min_month * Decimal(cssz_main_months))
    min_secondary_assessment = ceil_whole(cssz_secondary_min_month * Decimal(cssz_secondary_months))
    cssz_assessment = Decimal("0")

    if cssz_mode == "secondary":
        if active_months and profit >= cssz_threshold:
            cssz_assessment = max(ceil_whole(profit * cssz_assessment_percent / Decimal("100")), min_secondary_assessment)
        else:
            warnings.append(f"ČSSZ vedlejší: zysk jest poniżej progu {format_decimal(cssz_threshold,0)} CZK, więc wyliczono 0 CZK.")
    elif cssz_mode == "main":
        cssz_assessment = max(ceil_whole(profit * cssz_assessment_percent / Decimal("100")), min_main_assessment)
    elif cssz_mode == "mixed":
        secondary_part_required = cssz_secondary_months > 0 and secondary_profit >= cssz_threshold
        if secondary_part_required:
            cssz_assessment = max(
                calc_main_assessment + calc_secondary_assessment,
                min_main_assessment + min_secondary_assessment,
                min_main_assessment + calc_secondary_assessment,
            )
        else:
            cssz_assessment = max(calc_main_assessment, min_main_assessment)
            if cssz_secondary_months:
                warnings.append(f"Część vedlejší ČSSZ: przypisany zysk {format_decimal(secondary_profit,0)} CZK jest poniżej progu {format_decimal(cssz_threshold,0)} CZK; składkę naliczono tylko za część hlavní.")
    else:
        cssz_assessment = ceil_whole(profit * cssz_assessment_percent / Decimal("100"))
    cssz = ceil_whole(cssz_assessment * cssz_rate / Decimal("100")) if cssz_assessment > 0 else Decimal("0")

    vzp_mode = settings.get("vzp_mode", "main")
    vzp_main_months, vzp_secondary_months = status_month_counts(settings, year, "vzp_mode", "vzp_main_from_date")
    vzp_assessment_percent = max(d("vzp_assessment_percent", "50"), Decimal("0"))
    vzp_rate = max(d("vzp_rate_percent", "13.5"), Decimal("0"))
    vzp_min_month = max(d("vzp_min_monthly_base", "24483.50"), Decimal("0"))
    vzp_assessment = profit * vzp_assessment_percent / Decimal("100")
    if vzp_mode == "main":
        vzp_assessment = max(vzp_assessment, vzp_min_month * Decimal(active_months))
    elif vzp_mode == "mixed":
        vzp_assessment = max(vzp_assessment, vzp_min_month * Decimal(vzp_main_months))
    vzp = q2(vzp_assessment * vzp_rate / Decimal("100"))

    db = get_db()
    payment_rows = [dict(row) for row in db.execute("SELECT * FROM tax_payments WHERE substr(payment_date,1,4)=? ORDER BY payment_date DESC,id DESC", (str(year),)).fetchall()]
    paid = {key: Decimal("0") for key in TAX_PAYMENT_TYPES}
    for payment in payment_rows:
        try:
            paid[payment["payment_type"]] += parse_decimal(payment["amount"])
        except (KeyError, ValueError):
            warnings.append("Pominięto nieprawidłowy zapis wpłaty.")
    paid = {key: q2(value) for key, value in paid.items()}

    # V6: pełne rozliczenie podatku dochodowego.
    # Od szacowanego podatku rocznego po uldze odejmujemy zaliczki pobrane
    # przez Accenture oraz dodatkowe wpłaty podatku zapisane w aplikacji.
    income_tax_paid_total = q2(employment_tax_withheld + paid.get("income_tax", Decimal("0")))
    income_tax_balance = q2(annual_tax_after_credit - income_tax_paid_total)
    income_tax_underpayment = q2(max(income_tax_balance, Decimal("0")))
    income_tax_overpayment = q2(max(-income_tax_balance, Decimal("0")))
    income_tax = income_tax_underpayment

    obligations = {"income_tax": annual_tax_after_credit, "cssz": cssz, "vzp": vzp}
    remaining = {
        "income_tax": income_tax_underpayment,
        "cssz": q2(max(cssz - paid.get("cssz", Decimal("0")), Decimal("0"))),
        "vzp": q2(max(vzp - paid.get("vzp", Decimal("0")), Decimal("0"))),
    }
    total_obligation = q2(annual_tax_after_credit + cssz + vzp)
    total_paid = q2(income_tax_paid_total + paid.get("cssz", Decimal("0")) + paid.get("vzp", Decimal("0")))
    remaining_total = q2(sum(remaining.values(), Decimal("0")))
    after_obligations = q2(revenue - remaining_total)
    if basis == "paid" and not invoice_rows:
        warnings.append("Wybrano przychód według zapłaty. Oznacz faktury jako opłacone i wpisz datę otrzymania pieniędzy.")
    return {
        "year": year, "settings": settings, "invoice_rows": invoice_rows, "warnings": warnings,
        "revenue": revenue, "flat_expenses": flat_expenses, "profit": profit,
        "income_tax": income_tax,
        "annual_tax_before_credit": annual_tax_before_credit,
        "annual_tax_after_credit": q2(annual_tax_after_credit),
        "employment_tax_withheld": employment_tax_withheld,
        "income_tax_paid_total": income_tax_paid_total,
        "income_tax_balance": income_tax_balance,
        "income_tax_underpayment": income_tax_underpayment,
        "income_tax_overpayment": income_tax_overpayment,
        "cssz": q2(cssz), "vzp": vzp, "cssz_threshold": q2(cssz_threshold),
        "cssz_assessment": q2(cssz_assessment), "vzp_assessment": q2(vzp_assessment),
        "active_months": active_months,
        "cssz_main_months": cssz_main_months, "cssz_secondary_months": cssz_secondary_months,
        "vzp_main_months": vzp_main_months, "vzp_secondary_months": vzp_secondary_months,
        "paid": paid, "payments": payment_rows, "remaining": remaining,
        "total_obligation": total_obligation, "total_paid": total_paid,
        "remaining_total": remaining_total, "after_obligations": after_obligations,
    }


@app.route("/settings")
def settings_page() -> str:
    try:
        year = int(request.args.get("year", date.today().year))
        if year < 2020 or year > 2100:
            raise ValueError
    except ValueError:
        year = date.today().year
    snapshot = compute_tax_snapshot(year)
    return render_template("settings.html", snapshot=snapshot)


@app.route("/taxes")
def taxes_dashboard() -> str:
    try:
        year = int(request.args.get("year", date.today().year))
        if year < 2020 or year > 2100:
            raise ValueError
    except ValueError:
        year = date.today().year
    snapshot = compute_tax_snapshot(year)
    return render_template("taxes_dashboard.html", snapshot=snapshot, payments=snapshot["payments"], today=date.today().isoformat())


@app.post("/taxes/settings")
def tax_settings_save():
    try:
        year = int(request.form.get("year", date.today().year))
        values = {
            "activity_start_date": _valid_date_or_blank(request.form.get("activity_start_date", ""), "początek działalności"),
            "activity_end_date": _valid_date_or_blank(request.form.get("activity_end_date", ""), "koniec działalności"),
            "cssz_main_from_date": _valid_date_or_blank(request.form.get("cssz_main_from_date", ""), "początek hlavní ČSSZ"),
            "vzp_main_from_date": _valid_date_or_blank(request.form.get("vzp_main_from_date", ""), "początek minimum VZP"),
            "revenue_basis": request.form.get("revenue_basis", "paid"),
            "cssz_mode": request.form.get("cssz_mode", "main"),
            "vzp_mode": request.form.get("vzp_mode", "main"),
        }
        allowed_modes = {"secondary", "mixed", "main", "custom"}
        if values["revenue_basis"] not in {"paid", "issued"} or values["cssz_mode"] not in allowed_modes or values["vzp_mode"] not in allowed_modes:
            raise ValueError("Nieprawidłowy tryb ustawień.")
        numeric = (
            "expense_percent", "expense_limit", "income_tax_credit", "employment_tax_base", "employment_tax_withheld", "tax_threshold",
            "cssz_threshold_annual", "cssz_threshold_reduction_month", "cssz_assessment_percent", "cssz_rate_percent",
            "cssz_min_monthly_base", "cssz_secondary_min_monthly_base", "cssz_monthly_advance",
            "vzp_assessment_percent", "vzp_rate_percent", "vzp_min_monthly_base", "vzp_monthly_advance",
        )
        for key in numeric:
            parsed = parse_decimal(request.form.get(key, "0"), key)
            if parsed < 0:
                raise ValueError(f"{key} nie może być ujemne.")
            values[key] = str(parsed)
    except (ValueError, TypeError) as exc:
        flash(str(exc), "error")
        return redirect(url_for("settings_page", year=request.form.get("year", date.today().year)))
    db = get_db()
    ensure_tax_settings(year)
    keys = list(values)
    db.execute("UPDATE tax_settings SET " + ",".join(f"{key}=?" for key in keys) + ",updated_at=CURRENT_TIMESTAMP WHERE year=?", tuple(values[key] for key in keys) + (year,))
    db.commit()
    flash("Ustawienia podatków i składek zostały zapisane.", "success")
    return redirect(url_for("settings_page", year=year))


@app.post("/taxes/payments/add")
def tax_payment_add():
    try:
        year = int(request.form.get("year", date.today().year))
        payment_date = _valid_date_or_blank(request.form.get("payment_date", ""), "data wpłaty")
        payment_type = request.form.get("payment_type", "")
        amount = parse_decimal(request.form.get("amount", ""), "kwota wpłaty")
        if not payment_date or payment_type not in TAX_PAYMENT_TYPES or amount <= 0:
            raise ValueError("Uzupełnij poprawnie dane wpłaty.")
    except (ValueError, TypeError) as exc:
        flash(str(exc), "error"); return redirect(url_for("taxes_dashboard", year=request.form.get("year", date.today().year)))
    get_db().execute("INSERT INTO tax_payments (payment_date,payment_type,amount,note) VALUES (?,?,?,?)",
                     (payment_date, payment_type, str(amount), request.form.get("note", "").strip()))
    get_db().commit(); flash("Wpłata została zapisana.", "success")
    return redirect(url_for("taxes_dashboard", year=year))


@app.post("/taxes/payments/<int:payment_id>/delete")
def tax_payment_delete(payment_id: int):
    db = get_db(); db.execute("DELETE FROM tax_payments WHERE id=?", (payment_id,)); db.commit()
    flash("Wpłata została usunięta.", "success")
    return redirect(url_for("taxes_dashboard", year=request.form.get("year", date.today().year)))


@app.route("/dph")
def dph_dashboard() -> str:
    today = date.today()
    try:
        year = int(request.args.get("year", today.year)); month = int(request.args.get("month", today.month))
        if month not in range(1,13): raise ValueError
    except ValueError: year, month = today.year, today.month
    report = build_dph_month_report(year, month); db = get_db()
    filing_row = db.execute("SELECT * FROM dph_filings WHERE period_year=? AND period_month=? AND filing_kind='SHV'", (year, month)).fetchone()
    filing = dict(filing_row) if filing_row else None
    start, end = month_bounds(year, month)
    expense_rows = db.execute("SELECT * FROM expenses WHERE expense_date BETWEEN ? AND ? AND dph_obligation IN ('foreign_service','review') ORDER BY expense_date", (start, end)).fetchall()
    foreign_expenses = []
    for row in expense_rows:
        item = dict(row); item["amount_czk"] = expense_value_czk(item) or Decimal("0"); foreign_expenses.append(item)
    return render_template("dph_dashboard.html", selected_year=year, selected_month=month, period_label=period_label(year, month),
        report=report, due_date=dph_due_date(year, month).isoformat(), filing=filing, today=today.isoformat(),
        foreign_expenses=foreign_expenses, moje_dane_url=MOJE_DANE_SHV_URL, vies_url=VIES_URL)


@app.route("/dph/shv/<int:year>/<int:month>.xml")
def dph_export_shv(year: int, month: int):
    if month not in range(1,13): abort(404)
    report = build_dph_month_report(year, month)
    try: xml_data = build_shv_xml(year, month, report, get_company())
    except ValueError as exc:
        flash(str(exc), "error"); return redirect(url_for("dph_dashboard", year=year, month=month))
    return send_file(BytesIO(xml_data), mimetype="application/xml", as_attachment=True, download_name=f"DPHSHV_{year}_{month:02d}.xml")


@app.route("/dph/shv/<int:year>/<int:month>.csv")
def dph_export_csv(year: int, month: int):
    if month not in range(1,13): abort(404)
    report = build_dph_month_report(year, month); output = StringIO(); writer = csv.writer(output, delimiter=';')
    writer.writerow(["Kraj","VAT ID bez prefiksu","Kod plnění","Liczba transakcji","Wartość CZK"])
    for row in report["rows"]: writer.writerow([row["country_code"], row["vat_number"], 3, row["count"], int(row["value_czk"])])
    return Response('\ufeff' + output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename=SHV_{year}_{month:02d}.csv"})


@app.post("/dph/mark-filed")
def dph_mark_filed():
    try:
        year = int(request.form.get("year", "")); month = int(request.form.get("month", "")); date.fromisoformat(request.form.get("filed_date", ""))
        if month not in range(1,13): raise ValueError
    except ValueError:
        flash("Nieprawidłowe dane zgłoszenia.", "error"); return redirect(url_for("dph_dashboard"))
    db = get_db(); db.execute("""INSERT INTO dph_filings (period_year,period_month,filing_kind,status,filed_date,confirmation_number,notes)
        VALUES (?,?,'SHV','filed',?,?,?) ON CONFLICT(period_year,period_month,filing_kind) DO UPDATE SET status='filed',
        filed_date=excluded.filed_date,confirmation_number=excluded.confirmation_number,notes=excluded.notes,updated_at=CURRENT_TIMESTAMP""",
        (year, month, request.form.get("filed_date", ""), request.form.get("confirmation_number", "").strip(), request.form.get("notes", "").strip()))
    db.commit(); flash("Zgłoszenie oznaczono jako wysłane.", "success")
    return redirect(url_for("dph_dashboard", year=year, month=month))


@app.post("/dph/reopen-filing")
def dph_reopen_filing():
    year = int(request.form.get("year", date.today().year)); month = int(request.form.get("month", date.today().month))
    db = get_db(); db.execute("UPDATE dph_filings SET status='draft',updated_at=CURRENT_TIMESTAMP WHERE period_year=? AND period_month=? AND filing_kind='SHV'", (year, month)); db.commit()
    flash("Status zmieniono na niewysłane.", "success"); return redirect(url_for("dph_dashboard", year=year, month=month))

@app.route("/invoices")
def invoices_list() -> str:
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "").strip()
    sql = """
        SELECT i.*, c.name AS contractor_name
        FROM invoices i JOIN contractors c ON c.id = i.contractor_id
        WHERE 1 = 1
    """
    params: list[Any] = []
    if q:
        sql += " AND (i.invoice_number LIKE ? OR c.name LIKE ? OR c.vat_id LIKE ?)"
        like = f"%{q}%"
        params.extend([like, like, like])
    if status in {"paid", "unpaid"}:
        sql += " AND i.status = ?"
        params.append(status)
    sql += " ORDER BY i.issue_date DESC, i.id DESC"
    db = get_db()
    rows = db.execute(sql, params).fetchall()
    invoices: list[dict[str, Any]] = []
    for row in rows:
        invoice = dict(row)
        item_rows = db.execute("SELECT * FROM invoice_items WHERE invoice_id = ?", (invoice["id"],)).fetchall()
        totals = calculate_items([dict(item) for item in item_rows], invoice["tax_mode"])
        invoice["total"] = totals["total_gross"]
        invoices.append(invoice)
    return render_template("invoices_list.html", invoices=invoices, q=q, status=status)


def default_new_invoice(company: dict[str, Any], contractor_id: str = "") -> tuple[dict[str, Any], list[dict[str, Any]]]:
    today = date.today(); due = today + timedelta(days=int(company["default_due_days"]))
    invoice = {"id": None, "invoice_number": "", "contractor_id": contractor_id,
        "issue_date": today.isoformat(), "supply_date": today.isoformat(), "due_date": due.isoformat(),
        "currency": company["default_currency"], "language": company["default_language"],
        "tax_mode": company["default_tax_mode"], "payment_method": "bank_transfer", "variable_symbol": "",
        "order_number": "", "reverse_charge_note": company["default_reverse_charge_note"], "notes": "",
        "czk_rate": "1" if company["default_currency"] == "CZK" else "", "dph_category": "auto", "status": "unpaid"}
    return invoice, [{"description": "Prace spawalniczo-montażowe", "quantity": "1", "unit": "h", "unit_price": "", "vat_rate": "0"}]


@app.route("/invoices/new", methods=["GET", "POST"])
def invoice_new() -> str:
    db = get_db(); company = get_company()
    contractors = [dict(row) for row in db.execute("SELECT * FROM contractors ORDER BY name COLLATE NOCASE").fetchall()]
    projects = [dict(row) for row in db.execute("SELECT * FROM projects WHERE active=1 ORDER BY project_number COLLATE NOCASE").fetchall()]
    if request.method == "GET":
        invoice, items = default_new_invoice(company, request.args.get("contractor_id", ""))
        return render_template("invoice_form.html", invoice=invoice, items=items, contractors=contractors, projects=projects, company=company, is_edit=False)
    invoice, items = invoice_form_from_request(); errors = validate_invoice(invoice, items)
    if errors:
        for error in errors: flash(error, "error")
        return render_template("invoice_form.html", invoice=invoice, items=items, contractors=contractors, projects=projects, company=company, is_edit=False)
    try:
        number, year, sequence = next_invoice_identity(db, invoice["issue_date"], company["invoice_prefix"])
        variable = invoice["variable_symbol"] or f"{year}{sequence:03d}"
        cursor = db.execute("""INSERT INTO invoices (invoice_number, invoice_year, sequence_no, contractor_id,
            issue_date, supply_date, due_date, currency, language, tax_mode, payment_method, variable_symbol,
            order_number, reverse_charge_note, notes, czk_rate, dph_category, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unpaid')""",
            (number, year, sequence, int(invoice["contractor_id"]), invoice["issue_date"], invoice["supply_date"],
             invoice["due_date"], invoice["currency"], invoice["language"], invoice["tax_mode"], invoice["payment_method"],
             variable, invoice["order_number"], invoice["reverse_charge_note"], invoice["notes"], invoice["czk_rate"],
             invoice["dph_category"]))
        invoice_id = int(cursor.lastrowid)
        for pos, item in enumerate(items, 1):
            vat = "0" if invoice["tax_mode"] != "vat" else str(parse_decimal(item["vat_rate"]))
            db.execute("INSERT INTO invoice_items (invoice_id, position, description, quantity, unit, unit_price, vat_rate) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (invoice_id, pos, item["description"].strip(), str(parse_decimal(item["quantity"])), item["unit"].strip(), str(parse_decimal(item["unit_price"])), vat))
        db.commit()
    except Exception:
        db.rollback(); raise
    try:
        path = save_invoice_pdf_to_disk(invoice_id); flash(f"Faktura {number} została wystawiona i zapisana w: {path.parent}", "success")
    except Exception as exc:
        flash(f"Faktura {number} została wystawiona, ale zapis PDF nie powiódł się: {exc}", "error")
    created, _, totals = get_invoice_bundle(invoice_id); info = invoice_dph_info(created, totals)
    if info["kind"] == "eu_service": flash(f"DPH: faktura trafi do SH VIES za {info['period_label']}. Termin {date_pl_filter(info['due_date'])}.", "success")
    elif info["warning"]: flash(f"DPH: {info['warning']}", "error")
    return redirect(url_for("invoice_view", invoice_id=invoice_id))


@app.route("/invoices/<int:invoice_id>")
def invoice_view(invoice_id: int) -> str:
    invoice, items, totals = get_invoice_bundle(invoice_id)
    return render_template("invoice_detail.html", invoice=invoice, items=totals["items"], totals=totals,
                           company=get_company(), dph_info=invoice_dph_info(invoice, totals), today=date.today().isoformat())


@app.route("/invoices/<int:invoice_id>/edit", methods=["GET", "POST"])
def invoice_edit(invoice_id: int) -> str:
    db = get_db(); row = db.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if row is None: abort(404)
    existing = dict(row); company = get_company()
    contractors = [dict(r) for r in db.execute("SELECT * FROM contractors ORDER BY name COLLATE NOCASE").fetchall()]
    projects = [dict(r) for r in db.execute("SELECT * FROM projects WHERE active=1 ORDER BY project_number COLLATE NOCASE").fetchall()]
    if request.method == "GET":
        items = [dict(r) for r in db.execute("SELECT * FROM invoice_items WHERE invoice_id=? ORDER BY position,id", (invoice_id,)).fetchall()]
        return render_template("invoice_form.html", invoice=existing, items=items, contractors=contractors, projects=projects, company=company, is_edit=True)
    invoice, items = invoice_form_from_request(existing); errors = validate_invoice(invoice, items)
    if errors:
        for error in errors: flash(error, "error")
        return render_template("invoice_form.html", invoice=invoice, items=items, contractors=contractors, projects=projects, company=company, is_edit=True)
    db.execute("""UPDATE invoices SET contractor_id=?, issue_date=?, supply_date=?, due_date=?, currency=?, language=?,
        tax_mode=?, payment_method=?, variable_symbol=?, order_number=?, reverse_charge_note=?, notes=?, czk_rate=?,
        dph_category=?, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
        (int(invoice["contractor_id"]), invoice["issue_date"], invoice["supply_date"], invoice["due_date"], invoice["currency"],
         invoice["language"], invoice["tax_mode"], invoice["payment_method"], invoice["variable_symbol"], invoice["order_number"],
         invoice["reverse_charge_note"], invoice["notes"], invoice["czk_rate"], invoice["dph_category"], invoice_id))
    db.execute("DELETE FROM invoice_items WHERE invoice_id=?", (invoice_id,))
    for pos, item in enumerate(items, 1):
        vat = "0" if invoice["tax_mode"] != "vat" else str(parse_decimal(item["vat_rate"]))
        db.execute("INSERT INTO invoice_items (invoice_id, position, description, quantity, unit, unit_price, vat_rate) VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (invoice_id, pos, item["description"].strip(), str(parse_decimal(item["quantity"])), item["unit"].strip(), str(parse_decimal(item["unit_price"])), vat))
    db.commit()
    try:
        path = save_invoice_pdf_to_disk(invoice_id); flash(f"Faktura {existing['invoice_number']} została zaktualizowana. PDF: {path.parent}", "success")
    except Exception as exc:
        flash(f"Faktura została zaktualizowana, ale zapis PDF nie powiódł się: {exc}", "error")
    return redirect(url_for("invoice_view", invoice_id=invoice_id))


@app.post("/invoices/<int:invoice_id>/toggle-paid")
def invoice_toggle_paid(invoice_id: int):
    db = get_db()
    row = db.execute("SELECT status,paid_date FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if row is None:
        abort(404)
    if row["status"] == "paid":
        db.execute("UPDATE invoices SET status='unpaid',paid_date='',updated_at=CURRENT_TIMESTAMP WHERE id=?", (invoice_id,))
        message = "Faktura została oznaczona jako nieopłacona."
    else:
        paid_date = request.form.get("paid_date", date.today().isoformat()).strip()
        try:
            date.fromisoformat(paid_date)
        except ValueError:
            flash("Podaj prawidłową datę zapłaty.", "error")
            return redirect(request.referrer or url_for("invoice_view", invoice_id=invoice_id))
        db.execute("UPDATE invoices SET status='paid',paid_date=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (paid_date, invoice_id))
        message = "Faktura została oznaczona jako opłacona."
    db.commit(); flash(message, "success")
    return redirect(request.referrer or url_for("invoice_view", invoice_id=invoice_id))


@app.post("/invoices/<int:invoice_id>/delete")
def invoice_delete(invoice_id: int):
    db = get_db()
    row = db.execute("SELECT invoice_number FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    if row is None:
        abort(404)
    db.execute("DELETE FROM invoices WHERE id = ?", (invoice_id,))
    db.commit()
    flash(f"Faktura {row['invoice_number']} została usunięta. Numer nie zostanie użyty ponownie.", "success")
    return redirect(url_for("invoices_list"))


_FONT_CACHE: tuple[str, str, bool] | None = None


def register_pdf_fonts() -> tuple[str, str, bool]:
    global _FONT_CACHE
    if _FONT_CACHE is not None:
        return _FONT_CACHE

    env_regular = os.environ.get("INVOICE_FONT_REGULAR")
    env_bold = os.environ.get("INVOICE_FONT_BOLD")
    candidates = [
        (env_regular, env_bold),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf", "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"),
        (r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\arialbd.ttf"),
        (r"C:\Windows\Fonts\calibri.ttf", r"C:\Windows\Fonts\calibrib.ttf"),
        ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
        ("/Library/Fonts/Arial.ttf", "/Library/Fonts/Arial Bold.ttf"),
    ]
    for regular, bold in candidates:
        if regular and Path(regular).exists():
            bold_path = bold if bold and Path(bold).exists() else regular
            try:
                pdfmetrics.registerFont(TTFont("InvoiceFont", regular))
                pdfmetrics.registerFont(TTFont("InvoiceFontBold", bold_path))
                _FONT_CACHE = ("InvoiceFont", "InvoiceFontBold", True)
                return _FONT_CACHE
            except Exception:
                continue
    _FONT_CACHE = ("Helvetica", "Helvetica-Bold", False)
    return _FONT_CACHE


def pdf_safe_text(value: Any, unicode_ok: bool) -> str:
    text = str(value or "")
    if unicode_ok:
        return text
    replacements = {"ß": "ss", "ø": "o", "Ø": "O", "ł": "l", "Ł": "L"}
    for old, new in replacements.items():
        text = text.replace(old, new)
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def pdf_number(value: Any, language: str, decimals: int = 2) -> str:
    number = parse_decimal(value)
    text = f"{number:,.{decimals}f}"
    if language in {"pl", "de", "cs"}:
        text = text.replace(",", "X").replace(".", ",").replace("X", " ")
    return text


def pdf_date(value: str, language: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return value
    if language == "en":
        return parsed.strftime("%Y-%m-%d")
    return parsed.strftime("%d.%m.%Y")


def paragraph_text(value: Any, unicode_ok: bool) -> str:
    text = pdf_safe_text(value, unicode_ok)
    return escape(text).replace("\n", "<br/>")


def build_invoice_pdf(
    company: dict[str, Any],
    invoice: dict[str, Any],
    contractor: dict[str, Any],
    items: list[dict[str, Any]],
) -> BytesIO:
    language = invoice.get("language") if invoice.get("language") in PDF_LABELS else "en"
    labels = PDF_LABELS[language]
    font_regular, font_bold, unicode_ok = register_pdf_fonts()
    currency = invoice.get("currency", "EUR")
    totals = calculate_items(items, invoice["tax_mode"])

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=15 * mm,
        leftMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=18 * mm,
        title=f"{labels['invoice']} {invoice['invoice_number']}",
        author=company.get("name", ""),
    )

    styles = getSampleStyleSheet()
    base = ParagraphStyle(
        "InvoiceBase",
        parent=styles["Normal"],
        fontName=font_regular,
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#20252B"),
    )
    small = ParagraphStyle("InvoiceSmall", parent=base, fontSize=8, leading=10)
    tiny = ParagraphStyle("InvoiceTiny", parent=base, fontSize=7, leading=9)
    bold = ParagraphStyle("InvoiceBold", parent=base, fontName=font_bold)
    heading = ParagraphStyle(
        "InvoiceHeading",
        parent=base,
        fontName=font_bold,
        fontSize=22,
        leading=25,
        alignment=TA_RIGHT,
        textColor=colors.HexColor("#7A1735"),
    )
    subheading = ParagraphStyle(
        "InvoiceSubheading",
        parent=base,
        fontName=font_bold,
        fontSize=10,
        leading=12,
        textColor=colors.HexColor("#7A1735"),
    )
    right = ParagraphStyle("InvoiceRight", parent=base, alignment=TA_RIGHT)
    right_bold = ParagraphStyle("InvoiceRightBold", parent=bold, alignment=TA_RIGHT, fontSize=11)
    center_small = ParagraphStyle("InvoiceCenterSmall", parent=small, alignment=TA_CENTER)
    header_cell = ParagraphStyle(
        "InvoiceHeaderCell", parent=small, fontName=font_bold, fontSize=7.2, alignment=TA_CENTER,
        textColor=colors.white, leading=8
    )

    def P(value: Any, style: ParagraphStyle = base) -> Paragraph:
        return Paragraph(paragraph_text(value, unicode_ok), style)

    company_address = "\n".join(filter(None, [company.get("address"), " ".join(filter(None, [company.get("postal_code"), company.get("city")])), company.get("country")]))
    contractor_address = "\n".join(filter(None, [contractor.get("address"), " ".join(filter(None, [contractor.get("postal_code"), contractor.get("city")])), contractor.get("country")]))

    story: list[Any] = []

    top_table = Table(
        [
            [
                P(company.get("name", ""), ParagraphStyle("CompanyName", parent=bold, fontSize=14, leading=17)),
                P(labels["invoice"], heading),
            ],
            [
                P(company_address, small),
                P(f"{labels['invoice_no']}:\n{invoice['invoice_number']}", right_bold),
            ],
        ],
        colWidths=[102 * mm, 78 * mm],
    )
    top_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2 * mm),
            ]
        )
    )
    story.extend([top_table, Spacer(1, 5 * mm)])

    seller_lines = [company.get("name", ""), company_address]
    if company.get("company_id"):
        seller_lines.append(f"{labels['company_id']}: {company['company_id']}")
    if company.get("vat_id"):
        seller_lines.append(f"{labels['vat_id']}: {company['vat_id']}")
    if company.get("email"):
        seller_lines.append(f"{labels['email']}: {company['email']}")
    if company.get("phone"):
        seller_lines.append(f"{labels['phone']}: {company['phone']}")

    buyer_lines = [contractor.get("name", ""), contractor_address]
    if contractor.get("company_id"):
        buyer_lines.append(f"{labels['company_id']}: {contractor['company_id']}")
    if contractor.get("vat_id"):
        buyer_lines.append(f"{labels['vat_id']}: {contractor['vat_id']}")
    if contractor.get("email"):
        buyer_lines.append(f"{labels['email']}: {contractor['email']}")
    if contractor.get("phone"):
        buyer_lines.append(f"{labels['phone']}: {contractor['phone']}")

    parties = Table(
        [
            [P(labels["seller"], subheading), P(labels["buyer"], subheading)],
            [P("\n".join(filter(None, seller_lines))), P("\n".join(filter(None, buyer_lines)))],
        ],
        colWidths=[88 * mm, 88 * mm],
        hAlign="LEFT",
    )
    parties.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#C9CDD2")),
                ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#E1E4E8")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F4EAF0")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 3 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3 * mm),
            ]
        )
    )
    story.extend([parties, Spacer(1, 5 * mm)])

    info_rows = [
        [P(labels["issue_date"], bold), P(pdf_date(invoice["issue_date"], language)), P(labels["supply_date"], bold), P(pdf_date(invoice["supply_date"], language))],
        [P(labels["due_date"], bold), P(pdf_date(invoice["due_date"], language)), P(labels["payment_method"], bold), P(PDF_PAYMENT_METHODS.get(language, PDF_PAYMENT_METHODS["en"]).get(invoice["payment_method"], invoice["payment_method"]))],
    ]
    if invoice.get("order_number") or invoice.get("variable_symbol"):
        info_rows.append(
            [
                P(labels["order_no"], bold), P(invoice.get("order_number", "")),
                P(labels["variable_symbol"], bold), P(invoice.get("variable_symbol", "")),
            ]
        )
    info_table = Table(info_rows, colWidths=[39 * mm, 51 * mm, 39 * mm, 51 * mm])
    info_table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#D8DCE0")),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F7F7F7")),
                ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#F7F7F7")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 2.5 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2.5 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 2 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2 * mm),
            ]
        )
    )
    story.extend([info_table, Spacer(1, 6 * mm)])

    if invoice["tax_mode"] == "vat":
        header = [labels["item_no"], labels["description"], labels["quantity"], labels["unit"], labels["unit_price"], labels["vat_rate"], labels["net"], labels["gross"]]
        data: list[list[Any]] = [[P(cell, header_cell) for cell in header]]
        for index, item in enumerate(totals["items"], start=1):
            data.append(
                [
                    P(index, center_small),
                    P(item["description"], small),
                    P(pdf_number(item["quantity_decimal"], language, 2), right),
                    P(item.get("unit", ""), center_small),
                    P(f"{pdf_number(item['unit_price_decimal'], language)} {currency}", right),
                    P(pdf_number(item["vat_rate_decimal"], language, 2), right),
                    P(f"{pdf_number(item['net'], language)} {currency}", right),
                    P(f"{pdf_number(item['gross'], language)} {currency}", right),
                ]
            )
        item_table = LongTable(data, colWidths=[9 * mm, 57 * mm, 17 * mm, 14 * mm, 22 * mm, 13 * mm, 23 * mm, 25 * mm], repeatRows=1)
    else:
        header = [labels["item_no"], labels["description"], labels["quantity"], labels["unit"], labels["unit_price"], labels["net"]]
        data = [[P(cell, header_cell) for cell in header]]
        for index, item in enumerate(totals["items"], start=1):
            data.append(
                [
                    P(index, center_small),
                    P(item["description"], small),
                    P(pdf_number(item["quantity_decimal"], language, 2), right),
                    P(item.get("unit", ""), center_small),
                    P(f"{pdf_number(item['unit_price_decimal'], language)} {currency}", right),
                    P(f"{pdf_number(item['net'], language)} {currency}", right),
                ]
            )
        item_table = LongTable(data, colWidths=[9 * mm, 86 * mm, 18 * mm, 16 * mm, 25 * mm, 26 * mm], repeatRows=1)

    item_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#7A1735")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), font_bold),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#C9CDD2")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 1.5 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 1.5 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 2 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2 * mm),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#FAFAFA")]),
            ]
        )
    )
    story.extend([item_table, Spacer(1, 5 * mm)])

    if invoice["tax_mode"] == "vat":
        summary_rows = [
            [P(labels["net"], bold), P(f"{pdf_number(totals['total_net'], language)} {currency}", right)],
            [P(labels["vat"], bold), P(f"{pdf_number(totals['total_vat'], language)} {currency}", right)],
            [P(labels["total_due"], ParagraphStyle("TotalLabel", parent=bold, fontSize=12)), P(f"{pdf_number(totals['total_gross'], language)} {currency}", ParagraphStyle("TotalValue", parent=right_bold, fontSize=13, textColor=colors.HexColor('#7A1735')))],
        ]
    else:
        summary_rows = [
            [P(labels["total_due"], ParagraphStyle("TotalLabel2", parent=bold, fontSize=12)), P(f"{pdf_number(totals['total_gross'], language)} {currency}", ParagraphStyle("TotalValue2", parent=right_bold, fontSize=13, textColor=colors.HexColor('#7A1735')))],
        ]
    summary = Table(summary_rows, colWidths=[55 * mm, 45 * mm], hAlign="RIGHT")
    summary.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#C9CDD2")),
                ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#F4EAF0")),
                ("LEFTPADDING", (0, 0), (-1, -1), 3 * mm),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3 * mm),
                ("TOPPADDING", (0, 0), (-1, -1), 2.5 * mm),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5 * mm),
            ]
        )
    )
    story.extend([summary, Spacer(1, 5 * mm)])

    blocks: list[Any] = []
    if invoice["tax_mode"] == "reverse_charge":
        note = invoice.get("reverse_charge_note") or company.get("default_reverse_charge_note") or labels["reverse_charge_default"]
        note_table = Table([[P(note, bold)]], colWidths=[180 * mm])
        note_table.setStyle(
            TableStyle(
                [
                    ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#7A1735")),
                    ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FFF7FA")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4 * mm),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4 * mm),
                    ("TOPPADDING", (0, 0), (-1, -1), 3 * mm),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3 * mm),
                ]
            )
        )
        blocks.extend([note_table, Spacer(1, 4 * mm)])
    elif invoice["tax_mode"] == "no_vat":
        blocks.extend([P(labels["no_vat"], bold), Spacer(1, 3 * mm)])

    payment_lines = []
    if company.get("bank_name"):
        payment_lines.append(f"{labels['bank']}: {company['bank_name']}")
    if company.get("bank_account"):
        payment_lines.append(f"{labels['account']}: {company['bank_account']}")
    if company.get("iban"):
        payment_lines.append(f"{labels['iban']}: {company['iban']}")
    if company.get("bic"):
        payment_lines.append(f"{labels['bic']}: {company['bic']}")
    if invoice.get("variable_symbol"):
        payment_lines.append(f"{labels['variable_symbol']}: {invoice['variable_symbol']}")

    if payment_lines:
        payment_table = Table(
            [[P(labels["bank_details"], subheading)], [P("\n".join(payment_lines))]],
            colWidths=[180 * mm],
        )
        payment_table.setStyle(
            TableStyle(
                [
                    ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor("#D8DCE0")),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F7F7F7")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4 * mm),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4 * mm),
                    ("TOPPADDING", (0, 0), (-1, -1), 2.5 * mm),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5 * mm),
                ]
            )
        )
        blocks.extend([payment_table, Spacer(1, 4 * mm)])

    if invoice.get("notes"):
        notes_table = Table(
            [[P(labels["notes"], subheading)], [P(invoice["notes"])]] ,
            colWidths=[180 * mm],
        )
        notes_table.setStyle(
            TableStyle(
                [
                    ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor("#D8DCE0")),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F7F7F7")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4 * mm),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4 * mm),
                    ("TOPPADDING", (0, 0), (-1, -1), 2.5 * mm),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5 * mm),
                ]
            )
        )
        blocks.append(notes_table)
    if blocks:
        story.append(KeepTogether(blocks))

    footer_name = pdf_safe_text(company.get("name", ""), unicode_ok)
    footer_ids = " | ".join(filter(None, [
        f"IČO: {pdf_safe_text(company.get('company_id'), unicode_ok)}" if company.get("company_id") else "",
        f"DIČ: {pdf_safe_text(company.get('vat_id'), unicode_ok)}" if company.get("vat_id") else "",
    ]))

    def draw_footer(canvas, document):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#D8DCE0"))
        canvas.setLineWidth(0.4)
        canvas.line(15 * mm, 14 * mm, A4[0] - 15 * mm, 14 * mm)
        canvas.setFont(font_regular, 7.5)
        canvas.setFillColor(colors.HexColor("#666A70"))
        canvas.drawString(15 * mm, 9 * mm, f"{footer_name} | {footer_ids}".strip(" |"))
        page_text = f"{pdf_safe_text(labels['page'], unicode_ok)} {document.page}"
        canvas.drawRightString(A4[0] - 15 * mm, 9 * mm, page_text)
        canvas.restoreState()

    doc.build(story, onFirstPage=draw_footer, onLaterPages=draw_footer)
    buffer.seek(0)
    return buffer


WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_folder_name(value: str, fallback: str = "Bez nazwy") -> str:
    """Tworzy bezpieczną nazwę folderu również dla Windows."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", (value or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        cleaned = fallback
    if cleaned.upper() in WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned[:120]


def invoice_pdf_destination(invoice: dict[str, Any]) -> Path:
    contractor_folder = safe_folder_name(invoice.get("contractor_name", ""), "Nieznany kontrahent")
    try:
        year = str(date.fromisoformat(invoice["issue_date"]).year)
    except (KeyError, TypeError, ValueError):
        year = str(invoice.get("invoice_year") or "Bez roku")
    safe_number = re.sub(r"[^A-Za-z0-9._-]+", "_", invoice["invoice_number"])
    folder = INVOICE_PDF_DIR / contractor_folder / year
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"Faktura_{safe_number}.pdf"


def save_invoice_pdf_to_disk(invoice_id: int) -> Path:
    """Buduje aktualny PDF i zapisuje go w folderze danego kontrahenta."""
    invoice, items, _totals = get_invoice_bundle(invoice_id)
    company = get_company()
    contractor = {
        "name": invoice["contractor_name"],
        "address": invoice["contractor_address"],
        "postal_code": invoice["contractor_postal_code"],
        "city": invoice["contractor_city"],
        "country": invoice["contractor_country"],
        "company_id": invoice["contractor_company_id"],
        "vat_id": invoice["contractor_vat_id"],
        "email": invoice["contractor_email"],
        "phone": invoice["contractor_phone"],
    }
    buffer = build_invoice_pdf(company, invoice, contractor, items)
    destination = invoice_pdf_destination(invoice)
    data = buffer.getvalue()

    # Gdy zmieniono kontrahenta lub jego nazwę, usuń starą kopię tej samej faktury.
    for old_copy in INVOICE_PDF_DIR.rglob(destination.name):
        try:
            if old_copy.resolve() != destination.resolve():
                old_copy.unlink()
        except OSError:
            pass

    temp_path = destination.with_name(destination.name + ".tmp")
    temp_path.write_bytes(data)
    os.replace(temp_path, destination)
    return destination


@app.route("/invoices/<int:invoice_id>/pdf")
def invoice_pdf(invoice_id: int):
    invoice, items, _totals = get_invoice_bundle(invoice_id)
    company = get_company()
    contractor = {
        "name": invoice["contractor_name"],
        "address": invoice["contractor_address"],
        "postal_code": invoice["contractor_postal_code"],
        "city": invoice["contractor_city"],
        "country": invoice["contractor_country"],
        "company_id": invoice["contractor_company_id"],
        "vat_id": invoice["contractor_vat_id"],
        "email": invoice["contractor_email"],
        "phone": invoice["contractor_phone"],
    }
    buffer = build_invoice_pdf(company, invoice, contractor, items)
    destination = invoice_pdf_destination(invoice)
    data = buffer.getvalue()
    temp_path = destination.with_name(destination.name + ".tmp")
    temp_path.write_bytes(data)
    os.replace(temp_path, destination)
    buffer.seek(0)

    safe_number = re.sub(r"[^A-Za-z0-9._-]+", "_", invoice["invoice_number"])
    return send_file(
        buffer,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"Faktura_{safe_number}.pdf",
    )


@app.route("/tools/open-invoices-folder")
def open_invoices_folder():
    INVOICE_PDF_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if CLOUD_MODE:
            flash("W wersji online folder znajduje się na serwerze. PDF pobieraj z widoku konkretnej faktury.", "success")
        elif os.name == "nt":
            os.startfile(str(INVOICE_PDF_DIR))  # type: ignore[attr-defined]
            flash("Otworzono folder faktur.", "success")
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(INVOICE_PDF_DIR)])
            flash("Otworzono folder faktur.", "success")
        else:
            subprocess.Popen(["xdg-open", str(INVOICE_PDF_DIR)])
            flash("Otworzono folder faktur.", "success")
    except Exception as exc:
        flash(f"Nie udało się otworzyć folderu. Ścieżka: {INVOICE_PDF_DIR}. Błąd: {exc}", "error")
    return redirect(url_for("tools_page"))



def ensure_daily_backup() -> None:
    """Jedna automatyczna kopia bazy na dzień; zachowuj 14 ostatnich."""
    if not DB_PATH.exists() or DB_PATH.stat().st_size == 0:
        return
    target = BACKUP_DIR / f"auto_{date.today().isoformat()}.sqlite3"
    if target.exists():
        return
    try:
        source = sqlite3.connect(DB_PATH)
        destination = sqlite3.connect(target)
        with destination:
            source.backup(destination)
        destination.close()
        source.close()
        backups = sorted(BACKUP_DIR.glob("auto_*.sqlite3"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in backups[14:]:
            try:
                old.unlink()
            except OSError:
                pass
        if REMOTE_DB_ENABLED:
            # 7 rotacyjnych slotów w chmurze, bez rosnącego zużycia miejsca.
            slot = date.today().weekday()
            upload_file_to_supabase(target, f"backups/slot_{slot}.sqlite3")
    except Exception as exc:
        print("Automatyczny backup nie powiódł się:", exc)


def create_database_backup(label: str = "backup") -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_label = re.sub(r"[^A-Za-z0-9_-]+", "_", label).strip("_") or "backup"
    destination = BACKUP_DIR / f"{safe_label}_{timestamp}.sqlite3"
    source = sqlite3.connect(DB_PATH)
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return destination


def validate_invoice_database(path: Path) -> tuple[bool, str]:
    required = {"company", "contractors", "invoice_sequences", "invoices", "invoice_items"}
    try:
        connection = sqlite3.connect(path)
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        connection.close()
    except sqlite3.Error as exc:
        return False, f"Nie można odczytać bazy SQLite: {exc}"
    tables = {row[0] for row in rows}
    missing = required - tables
    if missing:
        return False, "Wybrany plik nie jest bazą tej aplikacji. Brakuje tabel: " + ", ".join(sorted(missing))
    if not integrity or integrity[0] != "ok":
        return False, "Baza danych nie przeszła kontroli integralności."
    return True, "ok"


@app.route("/tools")
def tools_page() -> str:
    db = get_db()
    counts = {"invoices": db.execute("SELECT COUNT(*) AS c FROM invoices").fetchone()["c"],
              "contractors": db.execute("SELECT COUNT(*) AS c FROM contractors").fetchone()["c"],
              "expenses": db.execute("SELECT COUNT(*) AS c FROM expenses").fetchone()["c"]}
    sequences = db.execute("SELECT year,next_number FROM invoice_sequences ORDER BY year").fetchall()
    next_numbers = ", ".join(f"{row['year']}: {row['next_number']}" for row in sequences) or "1"
    return render_template(
        "tools.html",
        counts=counts,
        next_numbers=next_numbers,
        db_path=str(DB_PATH),
        invoices_path=str(INVOICE_PDF_DIR),
        remote_db_enabled=REMOTE_DB_ENABLED,
        remote_bucket=SUPABASE_BUCKET,
    )


@app.post("/tools/reset-invoices")
def reset_invoices():
    if request.form.get("confirmation", "").strip().upper() != "RESET":
        flash("Reset anulowany. Wpisz dokładnie RESET.", "error"); return redirect(url_for("tools_page"))
    db = get_db(); invoice_count = int(db.execute("SELECT COUNT(*) AS c FROM invoices").fetchone()["c"])
    contractor_count = int(db.execute("SELECT COUNT(*) AS c FROM contractors").fetchone()["c"])
    expense_count = int(db.execute("SELECT COUNT(*) AS c FROM expenses").fetchone()["c"])
    backup_path = create_database_backup("przed_resetem_faktur"); pdf_archive = None
    if INVOICE_PDF_DIR.exists() and any(INVOICE_PDF_DIR.iterdir()):
        pdf_archive = BACKUP_DIR / f"PDF_przed_resetem_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.move(str(INVOICE_PDF_DIR), str(pdf_archive)); INVOICE_PDF_DIR.mkdir(parents=True, exist_ok=True)
    try:
        db.execute("DELETE FROM invoice_items"); db.execute("DELETE FROM invoices"); db.execute("DELETE FROM invoice_sequences")
        db.execute("DELETE FROM dph_filings"); db.execute("DELETE FROM sqlite_sequence WHERE name IN ('invoices','invoice_items')"); db.commit()
    except Exception:
        db.rollback(); raise
    flash(f"Usunięto {invoice_count} faktur. Zachowano {contractor_count} kontrahentów oraz ustawienia podatków i składek. Kopia: {backup_path.name}." + (f" PDF: {pdf_archive}" if pdf_archive else ""), "success")
    return redirect(url_for("tools_page"))


@app.post("/tools/import-database")
def import_database():
    uploaded = request.files.get("database")
    if uploaded is None or not uploaded.filename:
        flash("Wybierz plik bazy danych.", "error")
        return redirect(url_for("tools_page"))

    suffix = Path(uploaded.filename).suffix.lower()
    if suffix not in {".db", ".sqlite", ".sqlite3"}:
        flash("Dozwolone są pliki .db, .sqlite i .sqlite3.", "error")
        return redirect(url_for("tools_page"))

    fd, temp_name = tempfile.mkstemp(prefix="faktury_import_", suffix=suffix, dir=DATA_DIR)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        uploaded.save(temp_path)
        valid, message = validate_invoice_database(temp_path)
        if not valid:
            flash(message, "error")
            return redirect(url_for("tools_page"))

        backup_path = create_database_backup("przed_importem") if DB_PATH.exists() else None
        current = g.pop("db", None)
        if current is not None:
            current.close()
        os.replace(temp_path, DB_PATH)
        init_db()
        init_tax_module()
        contractor_count = get_db().execute("SELECT COUNT(*) AS c FROM contractors").fetchone()["c"]
        backup_note = f" Poprzednia baza: {backup_path.name}." if backup_path else ""
        flash(f"Baza została zaimportowana. Wczytano {contractor_count} kontrahentów.{backup_note}", "success")
        return redirect(url_for("tools_page"))
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


@app.route("/backup")
def backup_database():
    db = get_db()
    db.commit()
    backup_path = create_database_backup("faktury_backup")
    return send_file(
        backup_path,
        as_attachment=True,
        download_name=f"faktury_backup_{date.today().isoformat()}.sqlite3",
    )


@app.errorhandler(404)
def not_found(_error):
    return render_template("404.html"), 404


def open_browser() -> None:
    webbrowser.open_new("http://127.0.0.1:5000")


# V7.1: wykonuj automatyczny backup dopiero po zdefiniowaniu wszystkich funkcji.
with app.app_context():
    ensure_daily_backup()


if __name__ == "__main__":
    if MIGRATED_FROM:
        print(f"Zaimportowano dane ze starej aplikacji: {MIGRATED_FROM}")
        print(f"Nowa baza danych: {DB_PATH}")
    print("Faktury OSVČ V7.3 FREE Web/PWA")
    print(f"Baza danych: {DB_PATH}")
    print(f"Faktury PDF: {INVOICE_PDF_DIR}")
    print(f"Supabase configured: {REMOTE_DB_ENABLED}; bucket={SUPABASE_BUCKET}; key_kind={SUPABASE_KEY_KIND}")
    port = int(os.environ.get("PORT", "5000"))
    if not CLOUD_MODE:
        threading.Timer(1.2, open_browser).start()
    elif not APP_PASSWORD:
        print("UWAGA: APP_PASSWORD nie jest ustawione - aplikacja online nie ma ochrony hasłem!")
    if CLOUD_MODE and not REMOTE_DB_ENABLED:
        print("UWAGA: SUPABASE_URL/SUPABASE_SERVICE_KEY nie są ustawione. Dane na darmowym Renderze NIE będą trwałe!")
    app.run(host="0.0.0.0" if CLOUD_MODE else "127.0.0.1", port=port, debug=False, use_reloader=False)
