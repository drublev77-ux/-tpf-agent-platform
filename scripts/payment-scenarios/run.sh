#!/bin/bash
# usage: run_pay.sh yookassa|bepaid-test|bepaid-live
MODE=$1; PORT=3120; B=http://localhost:$PORT
fuser -k 3120/tcp 2>/dev/null; sleep 1
PSQL="psql -q -h /tmp -p 5433 -U postgres paytest"
psql -q -h /tmp -p 5433 -U postgres -c "DROP DATABASE IF EXISTS paytest" -c "CREATE DATABASE paytest" 2>/dev/null
$PSQL -f db/schema.sql -c "CREATE TABLE tpf_secrets (key text primary key, value text not null); CREATE TABLE tpf_payments (payment_id TEXT PRIMARY KEY, provider TEXT NOT NULL, reference TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now());"
case $MODE in
  yookassa) $PSQL -c "INSERT INTO tpf_secrets VALUES ('pay_provider','yookassa'),('yookassa_shop_id','123456'),('yookassa_secret_key','test_abcdefghijklmnopqrstuv')";;
  bepaid-test) $PSQL -c "INSERT INTO tpf_secrets VALUES ('pay_provider','bepaid'),('bepaid_shop_id','361'),('bepaid_secret_key','b8647b68898b084b836474ed8d61ffe117c9a01168d867f24953b776ddcb134d'),('bepaid_test','1')";;
  bepaid-live) $PSQL -c "INSERT INTO tpf_secrets VALUES ('pay_provider','bepaid'),('bepaid_shop_id','361'),('bepaid_secret_key','b8647b68898b084b836474ed8d61ffe117c9a01168d867f24953b776ddcb134d'),('bepaid_test','0')";;
esac
echo '{}' > /tmp/paymock.json; : > /tmp/paymock.log
(NODE_OPTIONS="--require $(dirname "$0")/provider-mock.cjs" DATABASE_URL="postgres://postgres@127.0.0.1:5433/paytest" SESSION_SECRET=abc ADMIN_PASSWORD=s nohup npx next start -p $PORT > /tmp/payshop.log 2>&1 &); sleep 6
set_state(){ python3 - "$@" <<'PY'
import json,sys
s=json.load(open('/tmp/paymock.json')); pid=sys.argv[1]
if pid=='down': s['down']=sys.argv[2]=='1'
else: s[pid].update(json.loads(sys.argv[2]))
json.dump(s,open('/tmp/paymock.json','w'))
PY
}
status(){ curl -s $B/order/$1 | grep -o "status: <!-- -->[a-z]*" | sed 's/.*-->//'; }
echo "== $MODE: $(curl -s $B/ | grep -o 'payments: <!-- -->[a-z]*')"
R=$(curl -s -X POST $B/buy); TOKEN=$(echo $R | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])"); PID=$(echo $R | python3 -c "import sys,json;print(json.load(sys.stdin)['checkout']['sessionId'])")
echo "checkout: $(echo $R | python3 -c "import sys,json;print(json.load(sys.stdin)['checkout']['url'])")"
echo "request sent to provider: $(head -1 /tmp/paymock.log | cut -c1-520)"
echo "recorded: $($PSQL -Atc "select provider, reference='$TOKEN' from tpf_payments")"
if [ "$MODE" = yookassa ]; then NOTE='{"type":"notification","event":"payment.succeeded","object":{"id":"'$PID'","status":"succeeded","paid":true,"metadata":{"reference":"'$TOKEN'"}}}'; OK='{"status":"succeeded"}'; WRONG='{"status":"succeeded","amount":{"value":"1.00","currency":"RUB"}}'; RESET='{"status":"pending","amount":null}'
else NOTE='{"transaction":{"status":"successful","tracking_id":"'$TOKEN'"}}'; OK='{"status":"successful"}'; WRONG='{"status":"successful","order":{"currency":"RUB","amount":100}}'; RESET='{"status":"incomplete","order":null}'; fi
note(){ curl -s -o /dev/null -w "%{http_code}" -X POST $B/api/tpf-pay -H 'content-type: application/json' -d "$NOTE"; }
echo "1 forged notification, provider says unpaid: http $(note) -> $(status $TOKEN)"
echo "2 customer returns before paying: $(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' "$B/api/tpf-pay-return?ref=$TOKEN&to=/order/$TOKEN" | sed "s|$B||;s|$TOKEN|TOKEN|") -> $(status $TOKEN)"
set_state $PID "$WRONG"; echo "3 provider reports a different amount: http $(note) -> $(status $TOKEN)"
python3 - <<PY
import json; s=json.load(open('/tmp/paymock.json')); s['$PID'].pop('amount',None); s['$PID'].pop('order',None); s['$PID']['status']='pending'; json.dump(s,open('/tmp/paymock.json','w'))
PY
set_state down 1; echo "4 provider is down: http $(note) -> $(status $TOKEN)"; set_state down 0
if [ "$MODE" = bepaid-live ]; then set_state $PID '{"status":"successful","test":true}'; echo "5 a TEST payment on a live shop: http $(note) -> $(status $TOKEN)"; set_state $PID '{"test":false}'; fi
set_state $PID "$OK"; echo "6 provider confirms the payment, customer returns: $(curl -s -o /dev/null -w '%{http_code}' "$B/api/tpf-pay-return?ref=$TOKEN&to=/order/$TOKEN") -> $(status $TOKEN)"
echo "7 notification repeated: http $(note) -> $(status $TOKEN)"
echo "8 return with a foreign address: $(curl -s -o /dev/null -w '%{redirect_url}' "$B/api/tpf-pay-return?ref=$TOKEN&to=//evil.example/x" | sed "s|$B||") | $(curl -s -o /dev/null -w '%{redirect_url}' "$B/api/tpf-pay-return?ref=$TOKEN&to=https://evil.example" | sed "s|$B||")"
echo "9 unknown reference: http $(curl -s -o /dev/null -w "%{http_code}" -X POST $B/api/tpf-pay -H 'content-type: application/json' -d '{"object":{"metadata":{"reference":"nope"}}}'), provider calls so far: $(wc -l < /tmp/paymock.log)"
$PSQL -c "INSERT INTO tpf_secrets VALUES ('check_token','0123456789abcdef0123456789abcdef0123') ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value"
echo "10 platform payment check: $(curl -s -X POST $B/api/tpf-check -H 'x-tpf-check: 0123456789abcdef0123456789abcdef0123' | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['ok'], [s['ok'] for s in d['steps']])") ; without token: $(curl -s -o /dev/null -w '%{http_code}' -X POST $B/api/tpf-check)"
grep -i "error\|fail" /tmp/payshop.log | head -5
fuser -k 3120/tcp 2>/dev/null; true
