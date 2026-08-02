# WhatsApp spool kontrakt pro planner_v10

Tento dokument je závazný kontrakt mezi WhatsApp wrapperem, spool frontou a
`planner_v10/whatsapp_request_worker.py`. Vychází z dodaného manuálu
`analysis_results/AUTOMATION.md` a rozšiřuje jej o dohodnuté příkazy pro v10.

> Poznámka pro budoucí handoff: tento soubor není nutné načítat při každé práci
> na planneru. Načítat jej, pokud se mění WhatsApp wrapper, spool worker,
> request skripty nebo komunikace přes WhatsApp.

## 1. Cesty

Základ fronty na serveru:

```text
/home/automatization/goodwe/whatsapp-spool
```

Adresáře:

```text
/home/automatization/goodwe/whatsapp-spool/incoming/            nové validované požadavky
/home/automatization/goodwe/whatsapp-spool/processing/          požadavky převzaté ke zpracování
/home/automatization/goodwe/whatsapp-spool/done/                úspěšně dokončené požadavky
/home/automatization/goodwe/whatsapp-spool/failed/              neúspěšné požadavky
/home/automatization/goodwe/whatsapp-spool/outgoing/            nové odpovědi a notifikace
/home/automatization/goodwe/whatsapp-spool/outgoing-processing/ odchozí zprávy převzaté démonem
/home/automatization/goodwe/whatsapp-spool/outgoing-done/       odeslané zprávy
/home/automatization/goodwe/whatsapp-spool/outgoing-failed/     trvale neodeslané zprávy
```

Pomocné skripty:

```text
/home/automatization/goodwe/bin/reply_whatsapp.sh
/home/automatization/goodwe/bin/notify_admins.sh
```

Worker:

```text
/home/automatization/goodwe/planner_v10/whatsapp_request_worker.py
```

Request store pro planner:

```text
/home/automatization/goodwe/planner_v10/state/requests.json
```

## 2. Co garantuje vstupní WhatsApp filtr

Každý `*.json` soubor v `incoming/`:

- přišel z jediné nakonfigurované WhatsApp skupiny;
- prošel kontrolou odesílatele, je-li allowlist zapnutý;
- odpovídá právě jednomu známému pravidlu wrapperu;
- obsahuje jen validovaný interní příkaz a validované parametry;
- neobsahuje původní volný text uživatele;
- byl zapsán atomicky a není částečný.

Worker přesto znovu ověřuje:

```text
schema == 1
commands je seznam stringů a obsahuje právě jeden příkaz
request_id je neprázdný string
source.message_id je neprázdný string
source.sender_id je neprázdný string
```

## 3. Podporovaný slovník interních příkazů

Wrapper smí do `commands[0]` zapisovat jen tyto interní příkazy:

```text
status
charge car;<energie>kWh;<absolutní ISO-8601 deadline>
heat boiler;<absolutní ISO-8601 deadline>
additional load;<power>kW;<absolutní ISO-8601 start>;<absolutní ISO-8601 end>
requests
cancel <číslo požadavku ze seznamu requests>
```

Časy už musí být převedené na absolutní ISO 8601 hodnotu s časovým posunem.
Worker je znovu nepřepočítává podle aktuálního času.

### 3.1 Status

Interní příkaz:

```text
status
```

Chování workeru:

- asynchronně spustí pomocný proces, který zavolá `show_status.py`,
- pomocný proces pošle stdout jako odpověď na původní WhatsApp zprávu,
- nemění `state/requests.json`.

Příklad vstupu:

```json
{
  "schema": 1,
  "request_id": "4e3db10a-b7c0-49aa-bbd8-559684c4aabd",
  "received_at": "2026-07-22T17:01:12+02:00",
  "source": {
    "type": "whatsapp",
    "session": "default",
    "chat_id": "120363123456789012@g.us",
    "sender_id": "123456789012345@lid",
    "message_id": "false_120363123456789012@g.us_ABCDEF_123456789012345@lid"
  },
  "rule": "status",
  "commands": ["status"],
  "parameters": {}
}
```

### 3.2 Nabíjení auta

Interní příkaz:

```text
charge car;<energie>kWh;<absolutní ISO-8601 deadline>
```

Příklad:

```text
charge car;5.5kWh;2026-07-23T08:30:00+02:00
```

Příklad vstupu:

```json
{
  "schema": 1,
  "request_id": "db4a4d91-27ea-4667-8ca5-90a07c628065",
  "received_at": "2026-07-22T17:02:00+02:00",
  "source": {
    "type": "whatsapp",
    "session": "default",
    "chat_id": "120363123456789012@g.us",
    "sender_id": "123456789012345@lid",
    "message_id": "false_120363123456789012@g.us_ABC123_123456789012345@lid"
  },
  "rule": "charge-car",
  "commands": ["charge car;5.5kWh;2026-07-23T08:30:00+02:00"],
  "parameters": {
    "energy": "5.5",
    "deadline": "2026-07-23T08:30:00+02:00"
  }
}
```

Worker zapíše aktivní požadavek:

```json
{
  "id": "<request_id>",
  "type": "ev_charge",
  "created_at": "<received_at>",
  "available_from": "<received_at>",
  "deadline": "2026-07-23T08:30:00+02:00",
  "required_ac_kwh": 5.5,
  "status": "active",
  "source": "whatsapp",
  "request_id": "<request_id>"
}
```

Novější aktivní `ev_charge` nahradí starší aktivní `ev_charge`.

### 3.3 Nahřátí bojleru

Interní příkaz:

```text
heat boiler;<absolutní ISO-8601 deadline>
```

Příklad:

```text
heat boiler;2026-07-23T08:30:00+02:00
```

Příklad vstupu:

```json
{
  "schema": 1,
  "request_id": "bb0f1f4e-b78c-4263-90d8-eceb5f991abc",
  "received_at": "2026-07-22T17:10:00+02:00",
  "source": {
    "type": "whatsapp",
    "session": "default",
    "chat_id": "120363123456789012@g.us",
    "sender_id": "123456789012345@lid",
    "message_id": "false_120363123456789012@g.us_BOILER_123456789012345@lid"
  },
  "rule": "heat-boiler",
  "commands": ["heat boiler;2026-07-23T08:30:00+02:00"],
  "parameters": {
    "deadline": "2026-07-23T08:30:00+02:00"
  }
}
```

Worker zapíše aktivní požadavek:

```json
{
  "id": "<request_id>",
  "type": "boiler_full",
  "created_at": "<received_at>",
  "deadline": "2026-07-23T08:30:00+02:00",
  "status": "active",
  "source": "whatsapp",
  "request_id": "<request_id>"
}
```

Novější aktivní `boiler_full` nahradí starší aktivní `boiler_full`.

### 3.4 Ohlášená dodatečná plánovaná zátěž

Interní příkaz:

```text
additional load;<power>kW;<absolutní ISO-8601 start>;<absolutní ISO-8601 end>
```

Příklad:

```text
additional load;2.5kW;2026-07-23T10:00:00+02:00;2026-07-23T12:30:00+02:00
```

`power` je výkon v kW po dobu intervalu od–do. Celková energie v kWh z toho
vzniká v planneru podle délky překryvu s 15min sloty. Požadavek bez konce se
odmítne.

Příklad vstupu:

```json
{
  "schema": 1,
  "request_id": "c2df88f1-fcf2-4e83-8aa4-37c0fc2e2abc",
  "received_at": "2026-07-22T17:15:00+02:00",
  "source": {
    "type": "whatsapp",
    "session": "default",
    "chat_id": "120363123456789012@g.us",
    "sender_id": "123456789012345@lid",
    "message_id": "false_120363123456789012@g.us_LOAD_123456789012345@lid"
  },
  "rule": "additional-load",
  "commands": ["additional load;2.5kW;2026-07-23T10:00:00+02:00;2026-07-23T12:30:00+02:00"],
  "parameters": {
    "power_kw": "2.5",
    "start": "2026-07-23T10:00:00+02:00",
    "end": "2026-07-23T12:30:00+02:00"
  }
}
```

Worker zapíše aktivní požadavek:

```json
{
  "id": "<request_id>",
  "type": "additional_load",
  "created_at": "<received_at>",
  "power_kw": 2.5,
  "phase": null,
  "start": "2026-07-23T10:00:00+02:00",
  "end": "2026-07-23T12:30:00+02:00",
  "description": "whatsapp announced load",
  "status": "active",
  "source": "whatsapp",
  "request_id": "<request_id>"
}
```

Dodatečných zátěží může být aktivních více.

### 3.5 Výpis aktivních požadavků

Interní příkaz:

```text
requests
```

Worker načte aktivní položky ze `state/requests.json` a odpoví očíslovaným
seznamem. Čísla jsou dočasné 1-based identifikátory pro příkaz `cancel` a platí
pro aktuální stav seznamu v okamžiku odpovědi.

### 3.6 Zrušení požadavku

Interní příkaz:

```text
cancel <číslo>
```

Příklad:

```text
cancel 1
```

Worker zruší aktivní požadavek podle čísla ze seznamu `requests`:

- budoucí/neprobíhající požadavek označí v `state/requests.json` jako
  `status="cancelled"` a odpoví `ok`;
- pokud podle aktuálního forecastu/intervalu požadavek právě probíhá, také jej
  označí jako `cancelled`, nastaví `cancelled_running=true` a odpoví, že
  požadavek právě probíhá, ale i tak byl zrušen.

Po úspěšném přidání nebo zrušení požadavku worker asynchronně spustí mimořádný
běh `planner.py --dry-run --verbose`, aby se přepočetl 48h forecast. Worker na
doběh planneru nečeká.

## 4. Bezpečné zpracování requestu

Worker nikdy nezpracovává soubor přímo v `incoming/`. Nejdřív jej atomicky
přesune do `processing/`. Po dobu zpracování se zachová původní JSON, aby
`reply_whatsapp.sh` mohl přečíst korelaci (`request_id`, `source.message_id`,
`source.sender_id`).

Pravidla:

- nepoužívat `eval`, `sh -c` ani vyhodnocování řetězce jako kódu;
- příkaz parsovat podle pevného protokolu;
- `request_id` používat jako idempotency key;
- `source.*` nikdy nepoužívat jako shell argument pro akční skripty;
- technické detaily chyb zapisovat do logu, ne do původního requestu.

## 5. Odpovědi

Odpověď na původní WhatsApp zprávu se posílá přes:

```bash
printf '%s\n' 'Text odpovědi' |
/home/automatization/goodwe/bin/reply_whatsapp.sh \
    /home/automatization/goodwe/whatsapp-spool/processing/NAZEV_REQUESTU.json
```

Úspěšné přijetí požadavku odpoví lidským potvrzením, například:

```text
Rozumím. Předávám plánovači požadavek nahřát bojler do 2026-07-23T08:30:00+02:00.
```

Chybný nebo neznámý příkaz odpoví chybou a request se přesune do `failed/`,
například:

```text
Příkaz se nepodařilo zpracovat: Konec dodatečné zátěže musí být později než začátek.
```

Text `odesílání odloženo` z `reply_whatsapp.sh` není chyba. Položka už je v
odchozí frontě a nesmí se vytvářet duplicitně.

## 6. Cron

Worker je cron-friendly polling proces. Cron jej spouští každou minutu:

```cron
* * * * *       cd /home/automatization/goodwe/planner_v10; PYTHONDONTWRITEBYTECODE=1 python3 whatsapp_request_worker.py --quiet >> /home/automatization/goodwe/logs/whatsapp_request_worker.log 2>&1
```

Výchozí běh udělá 5 kontrol po 10 sekundách (`--poll-iterations 5`,
`--poll-interval-seconds 10`) a v každé kontrole zpracuje až 20 requestů
(`--max-requests 20`). Cron tedy zůstává minutový safety net, ale běžná odezva
je do cca 10 sekund. Parametr `--quiet` v cronu vypíná tisk na stdout, protože
worker zároveň zapisuje do stejného logu sám; bez něj by se řádky v logu
duplikovaly.
