# スモールキャップ銘柄スクリーニングツール README

`screen_small_caps.py` は J-Quants API を使って、次の条件に合う銘柄を抽出するためのスクリーニングツールです。

- プライム / スタンダード / グロース市場の銘柄
- 直近終値が過去52週高値を更新
- 直近出来高が、当日を除く20営業日平均出来高の2倍以上
- 時価総額が 200 億円以下
- 自己資本比率が 20% 以上

未指定時は「今日」を基準日にし、休場日であれば直近営業日にさかのぼって実行します。

## 1. 動作環境

- Python 3.11 以上を推奨
- J-Quants API の利用権限
- `JQUANTS_API_KEY`

依存パッケージは以下です。

```txt
jquants-api-client==2.0.1
```

## 2. 初回セットアップ

### 2-1. 仮想環境を作成する

```bash
python3 -m venv .venv
```

### 2-2. 仮想環境を有効化する

```bash
source .venv/bin/activate
```

### 2-3. 依存パッケージをインストールする

```bash
pip install -r requirements.txt
```

### 2-4. `.env` に API キーを設定する

`.env` ファイルに以下のように設定します。

```dotenv
JQUANTS_API_KEY=あなたのAPIキー
```

このプロジェクトでは `.env` は `.gitignore` に含まれているため、通常は Git にコミットされません。

## 3. 一番基本の実行方法

何も日付を指定せずに実行すると、その日の日時点で使える最新営業日を基準にスクリーニングします。

```bash
./.venv/bin/python screen_small_caps.py
```

## 4. 日付を指定して実行する方法

日付はすべて `yyyy-MM-dd` 形式で指定します。

### 4-1. 1日だけ指定する

```bash
./.venv/bin/python screen_small_caps.py --date 2026-04-07
```

### 4-2. 複数日を指定する

`--date` を複数回書けます。

```bash
./.venv/bin/python screen_small_caps.py --date 2026-04-03 --date 2026-04-07 --date 2026-04-08
```

### 4-3. カンマ区切りで複数日を指定する

```bash
./.venv/bin/python screen_small_caps.py --date 2026-04-03,2026-04-07,2026-04-08
```

### 4-4. 期間を指定する

開始日から終了日までを 1 日ずつ順番に実行します。

```bash
./.venv/bin/python screen_small_caps.py --from-date 2026-04-01 --to-date 2026-04-05
```

### 4-5. 個別日付と期間指定を組み合わせる

個別に見たい日と、まとめて見たい期間を同時に指定できます。
重複した日付は自動でまとめられます。

```bash
./.venv/bin/python screen_small_caps.py --date 2026-04-10 --from-date 2026-04-01 --to-date 2026-04-05
```

## 5. 休場日の扱い

指定日が土日祝などの休場日だった場合、その日以前の直近営業日に自動で補正して実行します。

例:

- `2026-04-11` を指定
- その日が休場日
- 実際の価格基準日は `2026-04-10` になる

実行時には、標準エラー出力に補正内容が表示されます。

## 6. よく使う実行コマンド集

### 最新営業日で実行

```bash
./.venv/bin/python screen_small_caps.py
```

### 2026-04-07 時点で実行

```bash
./.venv/bin/python screen_small_caps.py --date 2026-04-07
```

### 2026-04-01 から 2026-04-10 までまとめて実行

```bash
./.venv/bin/python screen_small_caps.py --from-date 2026-04-01 --to-date 2026-04-10
```

### 3営業日分を個別指定して実行

```bash
./.venv/bin/python screen_small_caps.py --date 2026-04-03 --date 2026-04-07 --date 2026-04-08
```

### 表示件数を 50 件に増やして実行

```bash
./.venv/bin/python screen_small_caps.py --date 2026-04-07 --limit 50
```

### 時価総額上限を 300 億円に変更して実行

```bash
./.venv/bin/python screen_small_caps.py --date 2026-04-07 --max-market-cap-oku 300
```

## 7. 出力内容

実行すると、主に次の2つが出力されます。

- ターミナルにスクリーニング結果の表を表示
- `output/` 配下に CSV を保存

CSV には、従来の列に加えて次の情報も含まれます。

- 業種
- `PER`
- `PBR`
- `業種平均PER`
- `業種平均PBR`

`業種平均PER` と `業種平均PBR` は、同じ基準日時点の対象市場ユニバースから、同業種銘柄の利用可能な値を平均して算出しています。
そのため、業種や銘柄によっては空欄になることがあります。

CSV ファイル名の例:

```txt
output/20260407_stocks.csv
```

複数日実行で、指定日が休場日のため別の営業日に補正された場合は、ファイル名に指定日情報が付くことがあります。

例:

```txt
output/20260410_from_20260411_stocks.csv
```

これは「`2026-04-11` を指定したが、実際の価格基準日は `2026-04-10` だった」という意味です。

### PER / PBR の補足

- `PER` は、最新の財務開示から取得できる EPS 系列を使って算出します
- 予想 EPS (`FEPS` / `FNCEPS`) があればそれを優先します
- 予想 EPS が無い場合は実績 EPS (`EPS` / `NCEPS`) を使います
- `PBR` は `BPS` または `NCBPS` を使って算出します
- 分母が取得できない場合や 0 以下の場合は空欄になります

## 8. 主なオプション

よく使うオプションを抜粋すると次のとおりです。

- `--date`: 単日または複数日を指定
- `--from-date`: 期間指定の開始日
- `--to-date`: 期間指定の終了日
- `--limit`: ターミナルに表示する件数
- `--max-market-cap-oku`: 時価総額上限を億円単位で指定
- `--lookback-days`: 発行済株式数の近似取得に使う財務データの遡及日数
- `--financial-history-days`: 四半期業績の算出に使う財務データの遡及日数
- `--backtrack-days`: 休場日補正の最大遡り日数

ヘルプを確認したい場合:

```bash
./.venv/bin/python screen_small_caps.py --help
```

## 9. エラーになったときの確認ポイント

### API キー関連のエラーが出る

`.env` に `JQUANTS_API_KEY` が設定されているか確認してください。
値そのものをターミナルに表示するのではなく、エディタで `.env` を開いて確認するのがおすすめです。

### パッケージが見つからない

依存パッケージを再インストールしてください。

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

### レート制限にかかった

しばらく待ってから再実行するか、対象日数を分けて実行してください。

例:

```bash
./.venv/bin/python screen_small_caps.py --from-date 2026-04-01 --to-date 2026-04-03
```

その後、続きの期間を実行します。

```bash
./.venv/bin/python screen_small_caps.py --from-date 2026-04-04 --to-date 2026-04-06
```

## 10. 補足

- `.cache/` にキャッシュが作成されます
- `output/` に CSV が出力されます
- `.env`、`.cache/`、`output/` は Git 管理対象外です

## 11. 最短手順まとめ

初回セットアップから実行までを最短でやる場合は、以下を順番にコピペすれば動かせます。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# .env に JQUANTS_API_KEY を設定してから実行
./.venv/bin/python screen_small_caps.py
```
git init
pyenv local 3.13.13
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python screen_small_caps.py

./.venv/bin/python screen_small_caps.py --from-date 2026-01-01 --to-date 2026-04-18