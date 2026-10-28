# Faktury OSVČ V8.10 FREE

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

Świeża instalacja nie zawiera domyślnych danych właściciela ani jego indywidualnego profilu podatkowego. Uzupełnij dane firmy i ustawienia przed wystawieniem pierwszej faktury; istniejąca baza zachowuje zapisane wartości.

## Roczny pakiet dla księgowego
Na stronie „Podatki i składki” lub „Prop firmy” wybierz rok i pobierz pakiet ZIP. Zawiera podsumowanie PDF
oraz osobne pliki CSV z fakturami, pozycjami faktur, payoutami, wydatkami, zapisanymi wpłatami, ustawieniami kalkulatora i zgłoszeniami DPH.
Powiązana z payoutem faktura jest oznaczona w eksporcie i nie jest liczona drugi raz w kalkulatorze.
Podsumowanie pokazuje bieżące ustawienia i ostrzeżenia aplikacji; księgowy powinien zweryfikować dokumenty oraz kwalifikację podatkową.

## Dzienna ewidencja godzin
W menu „Godziny” można zapisywać datę pracy, kontrahenta, rodzaj pracy, projekt, opis, liczbę godzin, stawkę i walutę. W „Ustawienia → Stawki godzinowe” można zapisać stawki ogólne oraz osobne wartości dla projektu, rodzaju pracy i waluty; stawka projektowa ma pierwszeństwo i jest podpowiadana przy nowym wpisie. Zaznaczone wpisy jednego kontrahenta i jednej waluty tworzą jedną zbiorczą pozycję faktury, której kwota jest sumą godzin pomnożonych przez stawki z poszczególnych wpisów. Szczegółowa ewidencja pozostaje w aplikacji i nie jest drukowana na fakturze; wpisy połączone z fakturą są blokowane przed edycją.

Na formularzu faktury można zaznaczyć przeniesienie wybranych wpisów do „Listy godzin”. Po wystawieniu faktury otwiera się szkic istniejącego Stundenzettela z datami i łącznym czasem z ewidencji. Ewidencja nie zawiera godzin rozpoczęcia, zakończenia ani przerw, więc formularz użyje wartości domyślnych i wymaga sprawdzenia rzeczywistych czasów przed zapisaniem lub podpisaniem listy.
Przed pierwszą migracją aplikacja tworzy i weryfikuje kopię bazy, a przy skonfigurowanym Supabase zapisuje ją również w prywatnym bucketcie. Import bazy uruchamia tę samą migrację po utworzeniu kopii zaimportowanego pliku.

## Ważne
Render Free usypia aplikację po bezczynności. Pierwsze wejście po uśpieniu może trwać około minuty.
Supabase Free może wstrzymać projekt po dłuższej bezczynności; można go wznowić z panelu Supabase.
