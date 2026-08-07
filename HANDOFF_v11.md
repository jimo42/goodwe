# Handoff – produkční plánovač FVE/baterie/bojleru v11 (MILP)

Aktualizováno: **2026-08-07 17:00 CEST**

Účel dokumentu: rychlé navázání práce na aktuálně platné produkční verzi **v11** bez historických duplicit.

## 1. Aktuální stav jednou větou

Na serveru běží samostatný produkční deployment `/home/automatization/goodwe/planner_v11/` s reálným ovládáním baterie i bojleru:
`dry_run=false`, `battery_write_enabled=true`, `boiler_write_enabled=true`.
Planner je read-only vůči zařízením; zápisy provádí executor přes write gates a read-back/health-checky.

## 2. Závazná pravidla pro navázání

- **Git workflow je nyní aktivní a povinný.** Po dokončení každého úkolu aktualizovat tento handoff, ověřit změny přiměřenými testy/kontrolami, a ze serverového repozitáře `/home/automatization/goodwe` udělat `git commit` + `git push` do `origin/main`.
- **Commitovat ze serveru jako source of truth.** Produkční strom na `homeserver` je autoritativní pro Git; lokální `remote_staging/` slouží jen pro přípravu/audit/deploy skripty a lokální editaci před nahráním na server.
- **Před každým commitem chránit citlivá data.** Do Gitu nesmí jít lokální configy, privátní síťové adresy, klíče, hesla/tokeny, logy, runtime `state`, generated data, backupy ani lokální utility; spoléhat na `.gitignore` a před commitem kontrolovat staged soubory.
- **Crontab na serveru editovat jen přes** `/home/automatization/crontab-text-edit`; tento soubor se každou minutu aplikuje jako reálný crontab.
- **Před limitem kontextu vždy nejdříve aktualizovat handoff**, teprve potom ukončovat okno/task.
- **Nevynucovat ruční produkční executor bez důvodu.** Nejprve kontrolovat logy/runtime/ledger.
- **`state/boiler_control_state.json` je provozní historie.** Nemaž a neresetuj ho bez explicitního rozhodnutí, jinak se poruší denní budget a odhad dodané energie.

### 2.1 Vzdálené operace na `homeserver`

Na `homeserver` je zakázáno posílat složené vzdálené shell příkazy:
`&&`, `;`, pipelines, subshell, multi-step one-linery ani jiné shellové konstrukce.

Jeden `ssh homeserver` smí spustit právě **jeden** vzdálený příkaz. Pokud je potřeba více kroků:

1. vytvořit lokální audit/deploy skript v `remote_staging/`,
2. samostatným příkazem jej nahrát přes `scp`,
3. samostatným příkazem spustit např. `ssh homeserver 'bash /tmp/script.sh'`,
4. po potvrzeném výsledku ho samostatným příkazem odstranit.

To platí i pro read-only ověřování.

### 2.2 Lokální PowerShell validace

Podle globálních pravidel: dlouhou PowerShell validační logiku nikdy nespouštět jako inline one-liner. Pokud příkaz obsahuje pole, nested quotes, backticky, Unicode, `try/catch`, nebo je delší než cca 200 znaků, napsat jej do dočasného `.ps1` a spustit jen jako:

```powershell
pwsh.exe -NoLogo -NoProfile -NonInteractive -File .\.cline-tmp\command.ps1
```

Skript má chytat výjimky, vypsat chybu a skončit `exit 0` při úspěchu nebo `exit 1` při chybě.

## 3. Prostředí a zdroje pravdy

### 3.1 Systém

- Domácí server: SSH alias `homeserver`.
- Produkční root: `/home/automatization/goodwe/`.
- Aktivní produkce: `/home/automatization/goodwe/planner_v11/`.
- Lokální canonical staging: `remote_staging/planner_v11/`.
- Python na serveru: 3.13.5 systémově, bez venv; systém je PEP 668 `externally-managed`.
- `scipy`, `numpy`, `PuLP 2.7.0` jsou nainstalované systémově a funkční.
- `pytest` není nainstalovaný; testy spouštět přes `tests/run_manual.py`.
- Pokud chybí jakékoli linuxové nebo pythonovské balíčky, **nikdy se je neznažit nainstalovat či obejít**.
  Místo toho si o instalaci uživatelovi.

### 3.2 Řídicí dokumenty

Dokumenty pro původní architekturu v10 jsou uložené na serveru v `/home/automatization/goodwe/docs/` a lokálně v `input_from_other_ai/`:

- `ARCHITECTURE_DESIGN_v10_FINAL.md`
- `CONTROL_LOGIC_SPEC_v10.yaml`
- `SERVER_IMPLEMENTATION_GUIDE_v10.md`
- `CONSISTENCY_CHECK.json`

Jsou stále užitečné jako architektonické/spec podklady, ale **aktivní runtime source of truth je v11 deployment**, jeho `config.toml`, stavové JSONy, logy a aktuální kód v `planner_v11`.

## 4. Aktivní deployment v11

### 4.1 Verze a adresáře

- Aktivní produkce: `/home/automatization/goodwe/planner_v11/`.
- Lokální staging: `c:\$jimo\sejfik\remote_staging\planner_v11\`.
- Aktuální ověřené verze:
  - `planner.py 1.5`, `MODEL_VERSION="11-planner-v1"`
  - `executor.py 2.7`, `MODEL_VERSION="11-executor-v1"`
  - `whatsapp_request_worker.py 1.5`
  - `lib/request_store.py 1.3`
  - `lib/alerting.py 1.1`
  - `lib/relay.py 1.1`
  - `lib/telemetry.py 1.1`
  - `lib/boiler_state.py 1.1`
  - `show_status.py 1.2`

### 4.2 Produkční gates

Aktivní `config.toml` v11 má uživatelem schválené produkční zápisy:

```toml
dry_run = false
battery_write_enabled = true
boiler_write_enabled = true
```

`planner.py` zůstává read-only vůči zařízením a vytváří 48h MILP forecast. Zápisy do baterie a bojleru provádí pouze `executor.py` přes příslušné gates, validaci forecastu, relay health-check, headroom a read-back.

### 4.3 Cron

Aktivní v11 cron v `/home/automatization/crontab-text-edit`:

- planner: `7 * * * *`
- executor: `3-58/5 * * * *`
- logy:
  - `/home/automatization/goodwe/logs/planner_v11.log`
  - `/home/automatization/goodwe/logs/executor_v11.log`

WhatsApp worker a daily report běží také z `planner_v11` a zapisují do odpovídajících `*_v11.log` souborů.

## 5. Baterie a GoodWe ECO mapování

Aktivní produkční mapování v11:

- `HOLD` → aktivní ECO blok s minimálním nabíjením `enabled=true`, `power_pct=-1`, `soc_pct=100`. Reálný test 2026-08-03 ukázal, že původní `power_pct=0` neblokuje self-use vybíjení; `charge 1 %` dává cca desítky wattů do baterie a je nejbližší praktický hold. SoC target je 100 %, aby HOLD působil i při aktuálním SoC nad běžným grid-charge limitem.
- `FORCE_CHARGE` → aktivní ECO blok se záporným procentem výkonu.
- `DISCHARGE_TO_GRID` → aktivní ECO blok s kladným procentem výkonu.
- `SELF_USE` / `DISCHARGE_TO_LOAD` → ECO blok vypnutý, GoodWe zůstává v load-following/self-use režimu.
- Od `executor.py v2.4` se do čtyř GoodWe ECO kanálů zapisují **jen nejbližší aktivní ECO segmenty** (`HOLD`, `FORCE_CHARGE`, `DISCHARGE_TO_GRID`), sousední stejné sloty se slučují a load-following sloty (`SELF_USE` / `DISCHARGE_TO_LOAD`) se přeskočí místo zápisu vypnutých 15min bloků.

Závazné pasti:

- **Nikdy nezapisovat `eco_mode_N_switch` samostatně.** Zapisovat jen celý 12bajtový `eco_mode_N` blok; `on_off` je uvnitř bloku.
- Nepoužívat `set_operation_mode(ECO_CHARGE/ECO_DISCHARGE)` z knihovny GoodWe, protože přepisuje `eco_mode_1` na 24/7 shortcut a vypíná switche 2–4.
- Rolling quartet musí používat správný den v týdnu pro každý slot a nevytvářet okna přes půlnoc v jediném 12B bloku.

## 6. Bojler v11 – aktuální redesign

### 6.1 Denní budget a ledger

- `opportunistic_daily_limit_kwh=15.0` je samostatný limit pro každý lokální kalendářní den, nikoli rolling 48h limit.
- Planner pro dnešek odečítá z atomického ledgeru pouze `estimated_delivered_kwh`; budoucí dny začínají plným limitem.
- Hard požadavek `boiler_full` tímto oportunistickým limitem omezen není.
- `state/boiler_control_state.json` je atomický zdroj stavu pro:
  - `commanded_kwh`
  - `estimated_delivered_kwh`
  - poslední executor timestamp
  - masku fází
  - fázové transition timestampy
  - baseline, confidence a source
- Zapnuté relé samo o sobě neznamená dodanou energii. Minulý interval se účtuje z masky, baselines a minutové telemetrie před rozhodnutím/zápisem další masky.

### 6.2 Telemetrie

- Čtou se standardní minutové reporty `goodwe_stats_YYYYMMDD_HHMMSS` v `/home/automatization/goodwe/logs/goodwe-reports`.
- Nepoužívat `_full_` reporty jako zdroj pro tento redesign.
- Stable export je `min(latest, median)`.
- Pre-boiler surplus rekonstruuje jen potvrzený aktuální výkon bojleru.

### 6.3 Realtime ekonomika a fáze

- Executor každých 5 minut vyhodnocuje kandidáty 0–3 fáze přes `lib/economics.py`.
- Cena kombinuje ušlý export u části kryté stabilním přebytkem a plnou importní cenu u zbytku.
- Import je povolen jen když není levnější budoucí PV příležitost téhož dne; porovnává se s plynem `100 EUR/MWh = 2.46 CZK/kWh`.
- Executor smí ekonomicky překročit planner budget; hard cíl je vždy minimální povinný cíl.
- Maska může být libovolná, vybírají se nejméně zatížené bezpečné fáze.
- Platí 30 A soft limit, cca 2 kW baseline odečet, minimum on/off 5 minut a `rebalance_hysteresis_kw=0.3`.
- Pokud je současná maska bezpečná a přínos rotace je menší než hystereze, nerotuje se.
- Při změně se vždy vypíná před zapnutím.

### 6.4 Relé a read-back

- `/98` read-back je source of truth.
- Relé vrací správné čtyři status bajty `/98`, ale s chybným větším HTTP `Content-Length`.
- `lib/relay.py` v11 proto používá omezený socketový HTTP request tolerantní stejně jako existující `status.sh`/`curl`.
- HTTP timeout/`None` při shodném následném read-backu může být vyhodnocen jako úspěch.
- Zápis bojleru je chráněn úspěšným `/98` health-checkem, fázovým headroomem a read-backem.

## 7. Observabilita a stavové soubory

### 7.1 Důležité soubory ve `planner_v11/state/`

- `forecast_48h.json` – aktuální 48h MILP forecast.
- `runtime_state.json` – poslední executor runtime stav, včetně forecast validity, relay health, telemetry a rozhodnutí.
- `state_history.jsonl` – append-only historie executor běhů.
- `boiler_control_state.json` – atomický bojlerový ledger a provozní historie.
- `requests.json` – aktivní požadavky z WhatsApp/request store.
- `alert_state.json` – deduplikace alertů/reportů.

### 7.2 `show_status.py`

`show_status.py 1.2` rozlišuje mimo jiné:

- planner commanded/delivered/remaining,
- hard vs. opportunistic cíle po dnech,
- telemetry samples a age,
- min/median/latest/stable export,
- pre-boiler surplus,
- ekonomické kandidáty a future solar,
- current/target/confirmed masku,
- baseline/headroom,
- min-on/off,
- důvody planner/realtime/final rozhodnutí.

### 7.3 Meter sign convention

Potvrzená konvence GoodWe reportů:

- `meter_active_power_total` a `meter_active_power1/2/3`: **kladné = export**, **záporné = import**.
- `pbattery1`: **kladné = vybíjení**, **záporné = nabíjení**.
- `meter_e_total_exp/imp` jsou kumulativní hodnoty.

## 8. Ověření posledního známého stavu

### 8.1 Produkční přechod v11

- Uživatel schválil přechod z v10 na v11 s reálným ovládáním: `dry_run=false`, `battery_write_enabled=true`, `boiler_write_enabled=true`.
- Vytvořen samostatný adresář `/home/automatization/goodwe/planner_v11/`; původní v10 zůstává jen reference.
- Ostrý běh 2026-08-02 12:18 CEST:
  - planner vytvořil optimální forecast o 192 slotech (`dry_run=False`, SoC 86 %),
  - executor měl `forecast_valid=True`, `battery=eco_plan_written`,
  - read-back ověřil zápis všech čtyř kompletních `eco_mode_N` bloků a operaci `GENERAL → ECO`.
- Po opravě relé `Content-Length` problému:
  - serverové testy: **160/160 passed**,
  - přímé čtení relé vrátilo platné `0000`/`1100`,
  - ostrý executor 2026-08-02 13:36 CEST: `forecast_valid=True`, `battery=eco_plan_retained`, `boiler=relay_written`.

### 8.2 Audit po boiler redesignu

Nejnovější nezávislý audit proběhl **2026-08-02 22:21 CEST** v izolované kopii produkčního adresáře:

```bash
python3 -m compileall -q .
python3 tests/run_manual.py
```

Výsledek: **173/173 passed**.

Audit validoval:

- JSON kontrakt živého `runtime_state.json` a ledgeru,
- validní forecast,
- relay health OK,
- oba failure countery `0`,
- telemetry `sample_count >= 1`,
- třífázovou masku,
- synchronní `previous_executor_at` / `updated_at`.

Od upgradu v 20:58 měl executor `v2.2` jeden úvodní fail-safe běh kvůli forecastu vzniklému před změnou config hashe. Po novém planner runu následovala souvislá zdravá série; audit viděl 16 cyklů 21:03–22:18 s `forecast_valid=True`, `boiler=relay_written` a bez nového relay failure.

Produkční a lokální staging měly shodný SHA-256 pro všechny společné aktivní `*.py`/`*.toml`. Na serveru navíc existují jen historické artefakty mimo staging a mimo aktivní logiku:

- `backups/20260802_1140/config.toml`
- kořenové `inverter_client.py`
- kořenové `test_executor.py`

Nemazat je bez samostatného rozhodnutí; nejsou source of truth pro v11.

### 8.3 Oprava ECO zápisu přes půlnoc

Dne **2026-08-03 00:28 CEST** byl opraven problém se zápisem GoodWe ECO rolling quartet kolem půlnoci.

Zjištění z produkčních logů a `state/state_history.jsonl`:

- Zápisy začaly selhávat **2026-08-02 23:08 CEST** a selhávaly do **23:58 CEST**.
- Executor vracel `battery=eco_write_failed`, detail chyby byl `ILLEGAL DATA VALUE`.
- Všechny selhávající požadované rozvrhy obsahovaly blok `23:45 -> 00:00` s day bit pro neděli (`Sun`).
- Po půlnoci se zápisy samy obnovily, protože už vznikaly běžné bloky `00:00 -> 00:15`, `00:15 -> 00:30` atd. s day bit pro pondělí (`Mon`).

Oprava:

- `planner_v11/executor.py` byl nejdřív zvýšen na **v2.3**.
- Přidána funkce `eco_end_for_slot()`, která zamezí ECO blokům přes lokální půlnoc.
- Poslední plánovací slot dne se pro GoodWe zapisuje jako **`23:45 -> 23:59`** místo `23:45 -> 00:00`.
- První slot dalšího dne zůstává samostatný blok **`00:00 -> 00:15`** se správným weekday bitem následujícího dne.
- Přidány hermetické testy v `tests/test_executor.py` pro půlnoční truncation a weekday po půlnoci.

Ověření:

- Izolovaná candidate kopie `/tmp/planner_v11_eco_fix_candidate`: `python3 -m compileall -q .` + `python3 tests/run_manual.py` → **175/175 passed**.
- Aktivní deployment po nasazení: `python3 -m compileall -q .` + `python3 tests/run_manual.py` → **175/175 passed**.
- Reálný ruční zápis aktuálního ECO quartet plánu přes `inverter_client.write_eco_modes(..., dry_run=False)` proběhl s `status="written"`, `verified=true`.
- Následný aktivní executor smoke běh: `executor.py v2.3`, `forecast_valid=True`, `battery=eco_plan_retained`, `boiler=relay_written`.
- Záloha původních produkčních souborů je na serveru v `/home/automatization/goodwe/backups/planner_v11_eco_fix_20260803_002843/`.

### 8.4 Oprava ECO segmentů podle UI kontroly

Bezprostředně po opravě v2.3 už půlnoční zápisy fungovaly, ale uživatelská kontrola UI odhalila další návrhový problém:

- UI ukazovalo ECO mód, ale čtyři kanály byly většinou vypnuté 15min `DISCHARGE_TO_LOAD` bloky.
- Nejbližší plánovaný `HOLD` od `01:45` nebyl v UI vidět, protože původní rolling quartet pokrýval jen nejbližší hodinu a load-following sloty zapisoval jako disabled ECO okna.
- Technicky to nebyla okamžitá havárie: `DISCHARGE_TO_LOAD` / `SELF_USE` záměrně znamená vypnutý ECO blok a GoodWe load-following/self-use chování. Prakticky to ale bylo špatné UI i plánovací chování, protože aktivní budoucí HOLD se nevešel do čtyř kanálů.

Oprava nasazená **2026-08-03 00:48 CEST**:

- `planner_v11/executor.py` zvýšen na **v2.4**.
- `build_eco_quartet()` nyní programuje čtyři ECO kanály jako **nejbližší aktivní segmenty**, ne jako pevné čtyři 15min sloty.
- Load-following akce `DISCHARGE_TO_LOAD` / `SELF_USE` se přeskočí a nezabírají ECO kanály.
- Sousední stejné aktivní sloty se slučují do jednoho okna.
- Retenční logika rozpozná, že již zapsaný delší segment pořád pokrývá aktuální potřebu, takže se nemá zbytečně přepisovat každých 15 minut.
- Přidány testy pro:
  - přeskočení load-following slotů a naprogramování budoucího `HOLD`,
  - sloučení aktivních segmentů přes půlnoc bez vytvoření crossing bloku,
  - retenci dlouhého segmentu po posunu času.

Ověření:

- Izolovaná candidate kopie `/tmp/planner_v11_eco_segments_candidate`: `python3 -m compileall -q .` + `python3 tests/run_manual.py` → **176/176 passed**.
- Aktivní deployment po nasazení: `python3 -m compileall -q .` + `python3 tests/run_manual.py` → **176/176 passed**.
- Reálný zápis přes `inverter_client.write_eco_modes(..., dry_run=False)` proběhl s `status="written"`, `verified=true`.
- Read-back po zápisu potvrdil aktivní segmenty:
  - `eco_mode_1`: `01:45–04:45 Mon`, `on_off=-1`, `power=0`, `soc=0`.
  - `eco_mode_2`: `05:00–06:00 Mon`, `on_off=-1`, `power=0`, `soc=0`.
  - `eco_mode_3`: `22:30–23:59 Mon`, `on_off=-1`, `power=0`, `soc=0`.
  - `eco_mode_4`: `00:00–23:59 Tue`, `on_off=-1`, `power=0`, `soc=0`.
- Následný aktivní executor smoke běh: `executor.py v2.4`, `forecast_valid=True`, `battery=eco_plan_retained`, `boiler=relay_written`.
- Záloha původních v2.3 produkčních souborů je na serveru v `/home/automatization/goodwe/backups/planner_v11_eco_segments_20260803_004825/`.

### 8.5 HOLD skutečně nefungoval jako hold - oprava na minimální nabíjení

Bezprostředně po nasazení v2.4 uživatel provedl vlastní reálný test na střídači: nastavil aktivní `HOLD` (0 % ECO) od `00:45` a přesto se baterie dál vybíjela stejně jako v generickém self-use režimu. Ručně vyzkoušel `charge 1 %`, což dalo do baterie očekávaných cca 30 W.

Závěr: `power_pct=0` v GoodWe ECO **nezastaví self-use vybíjení**; funguje spíš jako no-op/self-use, ne jako skutečný hold. Nejbližší praktická náhrada holdu je minimální nucené nabíjení.

Oprava nasazená **2026-08-03 01:07 CEST**:

- `planner_v11/executor.py` zvýšen na **v2.5**.
- `HOLD` nyní mapuje na aktivní ECO blok `power_pct=-1` (charge 1 %) a `soc_pct=100`, ne na `power_pct=0`/`soc_pct=0`.
- SoC target je nastaven na `100`, ne na `max_soc_grid_pct-1`, aby hold fungoval i při aktuálním SoC nad běžným grid-charge limitem — cílem je jen zabránit self-use vybíjení, nikoli reálně dobít na 100 %.
- Upraven test `test_eco_mapping_for_charge_discharge_hold_and_load_following_actions` na nová očekávaná čísla `(-1, 100)`.

Ověření:

- Izolovaná candidate kopie `/tmp/planner_v11_hold_charge_candidate`: `python3 -m compileall -q .` + `python3 tests/run_manual.py` → **176/176 passed**.
- Aktivní deployment po nasazení: `python3 -m compileall -q .` + `python3 tests/run_manual.py` → **176/176 passed**.
- Reálný zápis přes `inverter_client.write_eco_modes(..., dry_run=False)` proběhl s `status="written"`, `verified=true`.
- Read-back po zápisu potvrdil `eco_mode_1`: `01:15–06:00 Mon`, `on_off=-1`, `power=-1`, `soc=100` (aktuální plán měl v tomto okamžiku jen jeden aktivní HOLD segment, zbylé tři kanály obsadily plánované `DISCHARGE_TO_GRID` sloty pro odpolední/večerní export).
- Následný aktivní executor smoke běh: `executor.py v2.5`, `forecast_valid=True`, `battery=eco_plan_retained`, `boiler=relay_written`.
- Záloha původních v2.4 produkčních souborů je na serveru v `/home/automatization/goodwe/backups/planner_v11_hold_charge_20260803_010735/`.

**Doporučená další kontrola pro navazujícího:** i po této opravě stojí za to nechat uživatele znovu potvrdit v UI/na displeji střídače, že se baterie během aktivního HOLD segmentu skutečně nevybíjí (mírné nabíjení cca desítek wattů je očekávané a v pořádku).

### 8.6 Falešné alerty `neočekávaná zátěž` při běžícím bojleru - oprava detektoru

Po prvním dni ostrého provozu uživatel nahlásil opakované WhatsApp alerty typu
`FVE ALERT: neočekávaná zátěž ...`, které časově korelovaly s reálným během
bojleru (např. 11:38, 11:43, 11:48, 11:53, 12:28, 12:38, 12:43 CEST dne
2026-08-03).

Root cause zjištěný z `state/state_history.jsonl` a kódu:

- `executor.py` nejdřív zapsal/ověřil bojlerovou masku a aktualizoval ledger,
  ale teprve potom volal `detectors.detect_loads()`.
- `lib/detectors.py v1.3` pro odečet bojleru používal jen heuristiku nad
  okamžitými `load_p1/2/3` a ignoroval již potvrzený runtime kontext bojleru.
- V některých bězích tak detektor současně viděl v ledgeru potvrzený aktivní
  bojler (`current_mask` / `confirmed_phase_delivery_kw`), ale přesto vyhodnotil
  `boiler.detected_kw = 0` a zbytek spotřeby chybně zařadil do
  `unexpected_load`.

Oprava nasazená **2026-08-03 22:19 CEST**:

- `planner_v11/executor.py` zvýšen na **v2.6**.
- `planner_v11/lib/detectors.py` zvýšen na **v1.4**.
- `detect_runtime_loads()` nyní předává do detektoru:
  - potvrzený `boiler_ledger`,
  - `telemetry_evidence`.
- `detect_loads()` nyní pro bojler preferuje tento source of truth v pořadí:
  1. `telemetry.confirmed_phase_delivery_kw`,
  2. `ledger.current_mask`,
  3. až potom fallback na původní heuristiku z `load_p1/2/3`.
- Výstup detektoru nově uvádí i `boiler.source` a `boiler.phase_kw`, aby bylo
  z runtime stavu vidět, z čeho byl bojlerový odečet odvozen.
- Doplněny regresní testy do `tests/test_detectors.py` pro:
  - preferenci confirmed ledger masky před heuristikou,
  - preferenci confirmed telemetry před ledger maskou.

Ověření:

- Izolovaná candidate kopie `/tmp/planner_v11_detector_fix_candidate`:
  `python3 -m compileall -q .` + `python3 tests/run_manual.py` →
  **179/179 passed**.
- Aktivní deployment po nasazení: `python3 -m compileall -q .` +
  `python3 tests/run_manual.py` → **179/179 passed**.
- Záloha původních produkčních souborů je na serveru v
  `/home/automatization/goodwe/backups/planner_v11_detector_fix_20260803_221911/`.

Doporučené provozní ověření po této opravě:

- sledovat další den `state/runtime_state.json`, `state/state_history.jsonl` a
  WhatsApp alerty,
- ověřit, že během aktivních bojlerových masek už nevznikají falešné alerty
  `executor.unexpected_load.*`, pokud nejde o skutečnou další zátěž.

## 9. WhatsApp/request/reporting stav


Aktivní WhatsApp worker běží z `planner_v11`.

Podporované interní příkazy ze spool workflow:

- `status`
- `requests`
- `cancel <číslo>`
- `charge car;<kWh>kWh;<ISO deadline>`
- `heat boiler;<ISO deadline>`
- `additional load;<kW>kW;<ISO start>;<ISO end>`

Request store je `state/requests.json`. Novější aktivní EV/bojler požadavek nahrazuje starší stejného typu; dodatečných zátěží může být aktivních více. Prošlé položky se označují jako `expired` a nevstupují do plánu.

Dokumentace wrapperu je lokálně v `WHATSAPP_AUTOMATION.md`; číst ji jen při práci na WhatsApp/spool/request automatizaci.

## 10. Důležité technické pasti

- Terminální hodnota v planneru nesmí být optimističtější než realistický import posledního slotu; jinak MILP může chtít dobíjet ze sítě jen kvůli terminálnímu bonusu.
- `economic_tie_tolerance_czk` dovoluje stage 3 mírně obětovat stage 2 ekonomiku; pro hraniční ekonomické testy nastavit toleranci na `0`.
- 15min slot při `inverter_total_kw=10` znamená max cca `2.5 kWh/slot`; větší jednoslotové zátěže mohou být fyzikálně infeasible.
- Na infeasible/non-optimal solver výsledek nesahat do `result.slots`; volající musí použít fail-safe.
- Nahrávání z Windows na server funguje přes `scp "c:\$jimo\sejfik\..." homeserver:/cesta/`.
- Lokální `python`/`python3` alias na Windows může být jen MS Store stub; produkční Python testování dělat na serveru přes popsaný script workflow, ne lokálními odhady.
- `del` s více argumenty v PowerShell nefunguje spolehlivě; použít `Remove-Item -LiteralPath 'a','b','c'`.

## 11. Doporučený bezpečný první krok při navázání

1. Přes script workflow zkontrolovat poslední řádky:
   - `/home/automatization/goodwe/logs/executor_v11.log`
   - `/home/automatization/goodwe/logs/planner_v11.log`
   - `planner_v11/state/runtime_state.json`
   - `planner_v11/state/boiler_control_state.json`
   - relay health uložený v runtime.
2. Neprovádět ruční executor s produkčními gates bez konkrétního důvodu.
3. Pokud se mění business logika:
   - nejdřív upravit lokální `remote_staging/planner_v11/`,
   - spustit hermetické testy,
   - ověřit izolovanou serverovou candidate kopii se shadow configem,
   - produkci zálohovat,
   - nasazovat přes script workflow,
   - znovu spustit isolated suite.
4. Při práci na relé/bojleru vždy zachovat health-check `/98`, read-back a ledger.
5. Při práci na baterii nikdy nezavádět nový GoodWe/ECO zápisový pattern bez ověření proti existujícím adaptérům a testům.

## 12. Co teď zbývá

- Pozorovat produkční v11 logy/runtime po boiler redesignu.
- Případné další změny business logiky dělat až po aktuálním auditu logů a přes výše uvedený script workflow.
- Po každém dokončeném úkolu aktualizovat handoff a následně provést serverový `git commit` + `git push` do větve main.

### EV fyzický limit a otevřená perzistence nabíjecí relace (audit 2026-08-07)

- Současné auto fyzicky nepřijme v jedné nabíjecí relaci více než **9 kWh AC**.
  Každý EV request se proto musí pro plánování interně omezit na
  `min(required_ac_kwh, 9.0)`, i když uživatel požádá o více. Původní uživatelskou
  hodnotu je vhodné ponechat auditně v requestu a do forecastu/reportingu přidat
  efektivní omezenou hodnotu a reason code.
- Audit aktivního requestu `48159406-edac-4931-b566-35374b3887e6` potvrdil, že
  planner po zahájení nabíjení dál plánuje celých původních 8 kWh. Oznámené okno
  bylo `12:15–15:15`; v `16:07` už bylo celé znovu posunuto na `16:00–19:00`,
  přestože executor odpoledne opakovaně detekoval EV zátěž.
- Root cause: executor zapisuje pouze okamžitý detektorový snapshot, planner tento
  stav ani již dodanou energii nenačítá a pro každý nový běh znovu předává solveru
  celé `required_ac_kwh`. Konfigurační `wallbox_energy_correction` se v aktuálním
  planneru/executoru nikde prakticky nepoužívá.
- Produkční wallbox API navíc není nakonfigurované
  (`wallbox API URL is not configured`), takže je momentálně k dispozici jen
  fázová heuristika. Ta není přesný elektroměr a v dnešních datech část L1 zátěže
  současně připsala EV i jedné fázi bojleru; před odečítáním kWh je nutné opravit
  ownership/dvojí započtení.
- **Zatím nenasazeno:** navržený směr je perzistentní `ev_session_state.json`,
  navázaný na `request_id`, se stavy `IDLE/ACTIVE/PAUSED/COMPLETED`, průběžnou
  energií a `last_positive_at`. Mezery do 30 minut patří do stejné relace
  (`PAUSED`); teprve souvislá mezera delší než 30 minut relaci ukončí. Planner má
  plánovat jen `max(0, effective_requested_kwh - delivered_kwh)` a během aktivní
  nebo krátce přerušené relace nesmí posouvat již přijaté/rozběhnuté okno.

## 13. Oprava WhatsApp replanu a finálního potvrzení (2026-08-03 22:59 CEST)

### Root cause potvrzený z produkčních logů

- EV request kolem `20:38` se správně uložil, ale mimořádný
  `planner.py --dry-run --verbose` skončil na přechodné kontrole chybějících
  aktivních cen.
- Starý worker nekontroloval exit code, neopakoval běh, neověřoval čerstvost ani
  korelaci forecastu a request po obecném potvrzení přesunul do `done/`.
- Pozdější regulérní planner run už neměl mechanismus, který by poslal chybějící
  WhatsApp doporučení.

### Nasazené chování

`planner_v11/whatsapp_request_worker.py VERSION="1.4"` nyní pro každý nový
`ev_charge`, `boiler_full` a `additional_load`:

1. request idempotentně uloží,
2. okamžitě odpoví, že je uložený a plán se přepočítává,
3. nechá claimed JSON v `whatsapp-spool/processing/`,
4. spustí detached helper a vynutí planner mimo cron,
5. retry provede až třikrát,
6. přijme jen forecast s `generated_at >= trigger` a stejným `request_id` v
   `active_requests`,
7. pošle druhé finální korelované potvrzení,
8. teprve potom přesune request do `done/`.

Pokud retry selžou, uživatel dostane explicitní finální zprávu a uložený request
zůstává aktivní pro další planner run.

### EV a formát odpovědí

- EV model dál vybírá právě jeden souvislý interval
  `EvCandidate.start_idx..end_idx`; finální zpráva obsahuje
  `recommended_start–expected_end` a `latest_safe_start`.
- Request store dovoluje jen jeden aktivní EV request, takže platí požadované
  jedno nabíjecí okno na den/request.
- Příkaz `requests` nově u EV zobrazí i timeframe z posledního matching forecastu.
- Uživatelské časy jsou `český den v týdnu D. M. YYYY HH:MM`, bez sekund a bez
  `+01:00` / `+02:00`.

### Ověření a rollback

- Candidate `compileall` + full suite: **185/185 passed**.
- Aktivní deployment `compileall` + full suite: **185/185 passed**.
- Backup před deployem:
  `/home/automatization/goodwe/backups/planner_v11_whatsapp_replan_20260803_225908/`.
- Testy nově kryjí immediate ACK + deferred finalization, retry, odmítnutí
  fresh-but-uncorrelated forecastu, EV timeframe, boiler/load potvrzení,
  timeframe v `requests`, user-facing time formatting a souvislost EV okna.
- Validace nevložila syntetický request do produkčního spoolu ani neměnila
  produkční `state/requests.json`.

## 14. WhatsApp alert při významné změně EV startu (2026-08-04 01:09 CEST)

### Nasazené chování

- `planner_v11/planner.py VERSION="1.5"` porovnává doporučený EV start s
  `ev_schedule_notification.last_notified_start` stejného aktivního
  `request_id`.
- Posun 0–59 minut včetně nic neposílá a referenci nemění. Absolutní posun
  alespoň 60 minut v obou směrech pošle přes `notify_admins.sh` celé nové okno
  `recommended_start–expected_end`.
- Po úspěšném odeslání se atomicky aktualizují pouze `last_notified_*`;
  `initial_notified_*` zůstávají auditně neměnné. Při chybě odeslání se
  reference nemění a další planner run provede retry.
- Malé změny se kumulují proti poslední oznámené referenci. Bez startu,
  infeasible doporučení nebo u expired/replaced/cancelled requestu se změnový
  alert neposílá; existující infeasibility alert zůstává.
- `whatsapp_request_worker.py VERSION="1.5"` zakládá baseline teprve po úspěšné
  finální korelované EV odpovědi.
- `lib/request_store.py VERSION="1.3"` a `lib/alerting.py VERSION="1.1"`
  serializují Linux `flock` kritické sekce. Překrývající se planner runy tak
  nemohou současně odeslat stejnou změnu a replace/cancel nemůže proběhnout
  mezi poslední kontrolou requestu a aktualizací reference.

### Ověření, migrace a rollback

- Izolovaný candidate: `compileall` + full suite **193/193 passed**.
- Aktivní produkční strom po instalaci: `compileall` + full suite
  **193/193 passed**.
- Produkční aktivní EV `request_id=28dfcf69-6034-4a0e-bc57-7d09ec6ed98a`
  byl bez odeslání zprávy inicializován z přesně matching feasible forecastu na
  baseline `2026-08-04T11:00:00+02:00`.
- Backup před nasazením:
  `/home/automatization/goodwe/backups/planner_v11_ev_schedule_alert_20260804_010914/`.
- Testy kryjí 59 minut, přesně 60 minut, oba směry, kumulaci malých změn,
  update po úspěchu, zachování reference a retry po chybě, chybějící baseline,
  infeasible/no-start, jiný a neaktivní request, neměnnost initial metadat a
  zápis prvního baseline pouze po úspěšné odpovědi.
- Nebyl vložen syntetický produkční spool request ani ručně spuštěn executor.

## 15. SoC odchylka a fail-safe timeout solveru (2026-08-06 14:05 CEST)

### Potvrzená interpretace a příčina incidentu

- Původní reason `SOC_DEVIATION_15.7_PCT_POINTS` obsahoval absolutní velikost
  odchylky. Směr se v reason ani WhatsApp textu nezobrazoval.
- Alert v `10:33` po replanu nebyl důkazem nové 27bodové chyby vzniklé za půl
  hodiny. Replan kolem `10:00` skončil po CBC limitu bez certifikovaného optima,
  takže produkční `forecast_48h.json` zůstal starý z `06:20`. Executor proto
  porovnal aktuální SoC 66 % se starým plánem 39 % a správně naměřil absolutní
  rozdíl 27 p. b.; problém byl ve stárnoucím plánu a příliš nízkém 10s limitu.

### Nasazené chování

- `executor.py VERSION="2.7"` používá konfigurovaný threshold
  `alerts.soc_deviation_threshold_pct_points = 15.0` (alert se spouští včetně
  přesně 15,0 p. b.). Machine reason je nyní například
  `SOC_DEVIATION_ABOVE_15.7_PCT_POINTS` nebo
  `SOC_DEVIATION_BELOW_27.0_PCT_POINTS`; WhatsApp má jednu stručnou řádku:
  `FVE ALERT: významná odchylka: SOC je o 15.7 % nad/pod plánem`.
- `solver.time_limit_seconds` je zvýšen z 10 na 60 sekund pro každou z až tří
  lexikografických fází MILP. Jde tedy o nejvýše 60 s na fázi, ne o jediný
  globální limit; typický živý běh po nasazení trval 3,9 s.
- `lib/solver_adapter.py` označí řešení jako `optimal` pouze při současném
  PuLP/CBC statusu `Optimal` a solution statusu `Optimal Solution Found`.
  Time-limit incumbent (`Integer Feasible`) se už nesmí vydávat za optimum.
- Pokud třetí tie-break fáze není optimální, `lib/optimizer.py` obnoví celý
  certifikovaný snapshot proměnných z optimální druhé fáze. Do forecastu se tak
  nemůže propsat částečný/necertifikovaný stage-3 incumbent pod statusem
  `optimal`. Neoptimální stage 1/2 zůstává fail-safe `not_solved` a planner
  nepřepíše poslední dobrý forecast.

### Ověření a rollback

- Candidate `compileall` + full suite: **197/197 passed**.
- Aktivní deployment `compileall` + full suite: **197/197 passed**.
- Živý candidate run nad aktuálními cenami, počasím, requesty a SoC:
  `optimal`, 192 slotů, 18,2 s.
- První nový produkční plán po nasazení:
  `generated_at=2026-08-06T14:04:54+02:00`, `optimal`, 192 slotů, 3,9 s;
  `forecast.config_hash` odpovídá nové konfiguraci.
- První normální cron cyklus po nasazení (`planner 14:07`, executor `14:08`):
  `forecast_valid=true`, bez validačních důvodů, SoC odchylka
  `SOC_DEVIATION_OK_ABOVE_0.0_PCT_POINTS`, relay health OK, ECO plán zapsán a
  ověřen, bojlerový read-back `relay_written`, oba failure countery `0`.
- Backup před deployem:
  `/home/automatization/goodwe/backups/planner_v11_soc_timeout_20260806_140448/`.
- Testy nově kryjí směry ABOVE/BELOW, hranici 14,9/15,0, stručný text,
  odmítnutí `Integer Feasible` jako optima a obnovení stage-2 hodnot po
  neoptimální stage 3.
- Executor nebyl ručně spuštěn; validace tedy nevyvolala dodatečný zápis do
  střídače ani relé mimo normální plánovaný cyklus.
