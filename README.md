# Faktury OSVČ V7.1 Web/PWA

Wersja mobilna i desktopowa tej samej aplikacji.

## Co nowego
- responsywny interfejs na telefon i laptop,
- PWA: można dodać ikonę na ekran główny,
- jedno hasło do aplikacji online,
- automatyczna kopia bazy raz dziennie (14 ostatnich),
- zapisywanie numerów projektów i wybór projektu przy wystawianiu faktury,
- zachowane moduły faktur, kontrahentów, podatków/ČSSZ/VZP i DPH/VIES.

## Lokalnie
```bash
pip install -r requirements.txt
python app.py
```

## Zmienne środowiskowe dla wersji online
- APP_PASSWORD - hasło do logowania
- SECRET_KEY - długi losowy sekret Flask
- APP_CLOUD_MODE=1
- INVOICE_APP_DATA=/var/data
- INVOICE_PDF_DIR=/var/data/invoices

## Render
Build command:
`pip install -r requirements.txt`

Start command:
`gunicorn app:app --workers 1 --threads 4 --timeout 120`

Dla SQLite koniecznie użyj jednego workera oraz persistent disk zamontowanego pod `/var/data`.


## V7.1
Poprawka uruchamiania automatycznych kopii bazy (backup jest wywoływany po zdefiniowaniu funkcji).
