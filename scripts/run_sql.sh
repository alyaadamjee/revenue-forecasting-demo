#!/bin/zsh
# Runs a demo SQL file statement by statement through the Databricks SQL Statements API (databricks CLI must be logged in).
# usage: DATABRICKS_WAREHOUSE_ID=<id> scripts/run_sql.sh <catalog.schema> sql/01_load_sources.sql
# {{schema}} -> catalog.schema, {{volume}} -> /Volumes/catalog/schema. Stops at the first failing statement.
set -e
schema=$1; file=$2; wh=${DATABRICKS_WAREHOUSE_ID:?set DATABRICKS_WAREHOUSE_ID}
volume="/Volumes/${schema/./\/}"
d=$(mktemp -d)
sed -e "s|{{schema}}|$schema|g" -e "s|{{volume}}|$volume|g" "$file" |
  perl -0ne 'my $i=0; for my $st (split /;\s*\n/) { $st=~s/^\s*(--[^\n]*\n\s*)*//; next unless $st=~/\S/; open(my $o,">","'$d'/".sprintf("%03d",$i++).".sql"); print $o $st; close $o }'
for q in $d/*.sql; do
  echo ">> $(head -c 100 $q | tr '\n' ' ')"
  perl -e 'local $/; my $q=<STDIN>; $q=~s/\\/\\\\/g; $q=~s/"/\\"/g; $q=~s/\t/ /g; $q=~s/\n/\\n/g; print qq({"warehouse_id":"$ARGV[0]","wait_timeout":"50s","format":"JSON_ARRAY","disposition":"INLINE","statement":"$q"})' "$wh" < $q > $q.json
  out=$(databricks api post /api/2.0/sql/statements --json @$q.json)
  if print -r -- "$out" | grep -q '"state": *"\(FAILED\|CANCELED\)"'; then
    print -r -- "$out" | perl -0ne 'print "FAILED: $1\n" if /"message":\s*"((?:[^"\\]|\\.)*)"/'; exit 1
  fi
done
echo "done: $file"
