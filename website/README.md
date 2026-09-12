# wandaklocek.pl — nowa strona

Statyczna strona wizytówka dla **Pracowni Rękodzieła Ludowego i Artystycznego
Wanda Klocek** w Nysie. Bez frameworków, bez kroku budowania — trzy pliki
i katalog z grafiką.

```
website/
├── index.html                 # cała treść strony (jedna strona z kotwicami)
└── assets/
    ├── css/styles.css         # style
    ├── js/main.js             # menu mobilne, animacje, podświetlanie menu
    └── img/                   # favicon + obrazek Open Graph (SVG)
```

## Uruchomienie lokalne

```bash
cd website
python3 -m http.server 8000
# http://localhost:8000
```

## Publikacja

Dowolny hosting plików statycznych. Wystarczy wgrać zawartość katalogu
`website/` do katalogu głównego serwera (`public_html`, `www` itp.).
Działa też na GitHub Pages, Netlify i Cloudflare Pages bez konfiguracji.

Po wgraniu warto ustawić przekierowania ze starych adresów:

| Stary adres | Nowy |
| --- | --- |
| `/opolska-porcelana-recznie-malowana.html` | `/#porcelana` |
| `/porcelana.html` | `/#porcelana` |
| `/kontakt.html` | `/#kontakt` |

## Co zmieniło się względem starej strony

- jedna strona zamiast kilku podstron `.html`, z menu kotwicowym,
- responsywność (telefon / tablet / desktop) i sticky menu,
- sekcja o historii wzoru opolskiego (kroszonki → 1963 → dziś),
- opis procesu zamawiania w czterech krokach,
- dane kontaktowe wyeksponowane: telefon klikalny, adres z linkiem do map,
- dane strukturalne `LocalBusiness` (JSON-LD) + Open Graph dla SEO
  i podglądu linków w mediach społecznościowych,
- dostępność: pomijanie do treści, widoczny focus, `prefers-reduced-motion`.

## Zdjęcia — do uzupełnienia

Strona nie zawiera fotografii wyrobów: nie udało się pobrać zdjęć ze starej
witryny. W miejscach przeznaczonych na zdjęcia są **rysowane motywy SVG**
(kwiat opolski, kroszonka, bombka, skrzynia, len), które wyglądają celowo,
ale docelowo warto je zastąpić prawdziwymi fotografiami.

Każde takie miejsce ma w HTML atrybut `data-photo` z opisem, co powinno się
tam znaleźć:

```bash
grep -n 'data-photo' index.html
```

Aby wstawić zdjęcie, wystarczy podmienić blok `<div class="card__art">…</div>`
na obrazek:

```html
<img class="card__art" src="assets/img/porcelana-kubki.jpg"
     alt="Kubki z porcelany malowane wzorem opolskim" loading="lazy" width="800" height="600">
```

`.card__art` ma już ustawione `aspect-ratio: 4/3`, więc kadr się nie rozjedzie.

## Treść do potwierdzenia przez właścicielkę

Treść zrekonstruowano na podstawie publicznie dostępnych informacji
(stara witryna była niedostępna do pobrania). Przed publikacją warto
sprawdzić poniższe:

- **rok 1992** jako początek działalności twórczej (sekcja „O pracowni"),
- **adres e-mail** — nie udało się go ustalić, na stronie jest tylko telefon
  i Facebook; jeżeli istnieje, dodać w sekcji „Kontakt" i w stopce,
- **godziny otwarcia** — obecnie widnieje formuła „odwiedziny prosimy umawiać
  telefonicznie"; jeśli pracownia ma stałe godziny, warto je wpisać,
- **NIP / dane rejestrowe** — celowo pominięte, źródła podawały sprzeczne
  numery; do uzupełnienia w stopce, jeśli mają być widoczne,
- **lista wyrobów** w sekcji „Kolekcje" i „Porcelana" — czy odpowiada
  aktualnej ofercie (formy, personalizacja, wysyłka kurierem).

Fakty o samym wzorze opolskim (wpis na krajową listę niematerialnego
dziedzictwa kulturowego w 2019 r., przeniesienie wzoru kroszonkarskiego
na porcelanę w 1963 r. w opolskiej Cepelii) pochodzą z opracowań
muzealnych i encyklopedycznych.
