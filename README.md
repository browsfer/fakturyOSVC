# Faktury OSVČ V7.2 FREE

Wariant w pełni darmowy:
- Render Free Web Service — uruchamia aplikację,
- Supabase Free Storage — przechowuje prywatny plik SQLite,
- brak Persistent Disk i brak płatnego planu Render.

## Render — Build Command
pip install -r requirements.txt

## Render — Start Command
gunicorn app:app --workers 1 --threads 4 --timeout 120

## Render — Environment Variables
APP_CLOUD_MODE=1
APP_PASSWORD=<twoje mocne hasło>
SECRET_KEY=<długi losowy sekret>
SUPABASE_URL=https://TWOJ_PROJECT.supabase.co
SUPABASE_SERVICE_KEY=<secret/service-role key>
SUPABASE_BUCKET=faktury-osvc

Nie ustawiaj INVOICE_APP_DATA=/var/data — w darmowej wersji nie używamy dysku Render.

## Supabase
1. Utwórz darmowy projekt.
2. Storage -> New bucket.
3. Nazwa: faktury-osvc.
4. Bucket ma pozostać PRIVATE.
5. W Settings/API skopiuj Project URL i server-side secret/service-role key.
6. Nigdy nie wrzucaj tego klucza do GitHub.

Aplikacja:
- pobiera SQLite z Supabase przy starcie,
- po zmianie danych wysyła nowy snapshot,
- przechowuje 7 rotacyjnych kopii bezpieczeństwa w folderze backups.

## Ważne
Render Free usypia aplikację po bezczynności. Pierwsze wejście po uśpieniu może trwać około minuty.
Supabase Free może wstrzymać projekt po dłuższej bezczynności; można go wznowić z panelu Supabase.
