#!/usr/bin/perl
# Generates the demo's fake input data (no real clients, people, IDs or amounts).
# Deterministic: the same seed always gives the same files.
# usage: perl scripts/generate_fake_data.pl data/
#
# Writes:
#   sf_opportunity.csv           ~ Salesforce opportunities (one row per deal, incl. open pipeline and renewals)
#   sf_opportunity_products.csv  ~ Salesforce opportunity products, one row per product (PID) x platform x market
#   contract_db.csv              ~ the contract database, one row per PID (won deals + open auto-renewals)
#   contract_db_monthly.csv      ~ contract database monthly blocks: base amount, active flag, revenue, Jan-25..Dec-27
#   teams.csv                   ~ teams, leaders and (fake, example.com) emails
use strict;
use warnings;
use POSIX qw(floor);

my $out = shift @ARGV || 'data';
srand(20261109);

sub pick { $_[int(rand(@_))] }
sub rnd  { my ($lo, $hi) = @_; $lo + rand($hi - $lo) }
sub ymd  { sprintf '%04d-%02d-%02d', @_ }
sub add_months { my ($y, $m, $n) = @_; my $t = $y * 12 + $m - 1 + $n; (floor($t / 12), $t % 12 + 1) }
sub last_day { my ($y, $m) = @_; my @d = (31, 28 + (($y % 4 == 0 && $y % 100) || $y % 400 == 0 ? 1 : 0), 31, 30, 31, 30, 31, 31, 30, 31, 30, 31); $d[$m - 1] }
sub csv { join(',', map { my $v = $_ // ''; $v =~ /[",\n]/ ? '"' . ($v =~ s/"/""/gr) . '"' : $v } @_) . "\n" }

# --- Teams ------------------------------------------------------------------------------------
my @teams = (
  [q{NA-North}, q{Jordan Ellis}, q{NA}], [q{NA-South}, q{Priya Raman}, q{NA}], [q{NA-East}, q{Marcus Webb}, q{NA}],
  [q{NA-West}, q{Sofia Navarro}, q{NA}], [q{NA-Central}, q{Daniel Okafor}, q{NA}], [q{EU-North}, q{Hannah Fischer}, q{EU}],
  [q{EU-South}, q{Luca Romano}, q{EU}],
);
open my $fs, '>', "$out/teams.csv" or die $!;
print $fs csv(qw(team team_leader region to_emails));
for my $s (@teams) {
  my $email = lc($s->[1] =~ s/ /./r) . '@example.com';
  print $fs csv($s->[0], $s->[1], $s->[2], $s->[0] eq q{NA-Central} ? q{} : $email);  # NA-Central: no recipient yet (app warns)
}
close $fs;

# --- Accounts (invented brands) ------------------------------------------------------------------
my @brands = qw(Zorvane Quillmoor Brindlewick Tavrello Mossbeck Ylvara Corvexa Plumwyn Grenvaly Halvestra
  Orbelle Fennix Drovane Kestaro Lumivo Marrowyn Nerovia Ostrel Pellacor Quorvy Rivandel Sorvik Tellumi Umbrisa
  Vexmoor Wyndrel Xandoro Yarrowby Zelquin Arvelle Bramtoft Cindrova Dalvesh Elmscar Faywick Glimmeron Hestovar
  Ilvoria Jandrel Korrowin);  # invented names, picked not to be real brands
my @kinds = ('Foods', 'Home', 'Beauty', 'Pet Care', 'Outdoor', 'Kitchen', 'Baby', 'Wellness', 'Electronics', 'Beverages');
my @accounts;  # [parent, team or undef, region]
my $na = 0;
for my $i (0 .. $#brands) {
  my $parent = "$brands[$i] " . pick(@kinds);
  my $team;
  if ($i < 3) { $team = undef }                       # 3 parents without a team -> "Unassigned"
  elsif ($i % 5 == 0 || $i % 7 == 0) { $team = pick(q{EU-North}, q{EU-South}) }
  else { $team = $teams[$na++ % 5][0] }
  push @accounts, [$parent, $team, ($team // '') =~ /^EU-/ ? q{EU} : q{NA}];
}

# --- Products --------------------------------------------------------------------------------------
my %platforms = (
  'Amazon Sponsored Ads' => ['Amazon'], 'Amazon DSP Media' => ['Amazon DSP'],
  'Walmart Sponsored Ads' => ['Walmart'], 'Other Marketplace Sponsored Ads' => ['Target', 'Instacart', 'Kroger'],
  'Retail Amazon' => ['Amazon'], 'Other DSP Media' => ['The Trade Desk'], 'Consultancy' => ['N/A'], 'Content' => ['N/A'],
);
my %product_name = (
  'Amazon Sponsored Ads' => 'Sponsored Ads Management', 'Amazon DSP Media' => 'DSP Managed Service',
  'Walmart Sponsored Ads' => 'Walmart Connect Management', 'Other Marketplace Sponsored Ads' => 'Omnichannel Retail Media',
  'Retail Amazon' => 'Amazon Retail Management', 'Other DSP Media' => 'Programmatic Media', 'Consultancy' => 'Strategy Consulting',
  'Content' => 'Content Optimisation',
);
my @categories = (('Amazon Sponsored Ads') x 5, ('Amazon DSP Media') x 2, ('Walmart Sponsored Ads') x 2,
  'Other Marketplace Sponsored Ads', 'Retail Amazon', 'Other DSP Media', 'Consultancy', 'Content');
sub pricing_for {
  my $c = shift;
  return pick('Retainer', 'One-Off') if $c eq 'Consultancy' || $c eq 'Content';
  return pick('% of Sales (Fixed)', '% of Sales (Tiered)', '% of COGS') if $c eq 'Retail Amazon';
  return pick('% of Media (Fixed)', '% of Media (Fixed)', '% of Media (Tiered)', 'Retainer');
}
my @owners = ('Taylor Brooks', 'Morgan Lee', 'Casey Patel', 'Riley Chen', 'Alex Moreau', 'Jamie Novak');

# --- Opportunities, products, contract database ----------------------------------------------------
my (@opps, @lines, @contract, @monthly);
my ($opp_n, $pid_n, $row_n) = (100000, 1000, 0);
my @months; for my $k (0 .. 35) { my ($y, $m) = add_months(2025, 1, $k); push @months, [$y, $m] }
my $refresh = '2026-11-09';

sub season { my $m = shift; { 1 => 0.85, 2 => 0.8, 3 => 0.9, 4 => 0.9, 5 => 0.95, 6 => 1.0, 7 => 1.35, 8 => 0.95, 9 => 1.0, 10 => 1.25, 11 => 1.5, 12 => 1.4 }->{$m} }

sub new_opp {
  my (%a) = @_;
  my $code = q{OPP-} . ++$opp_n;
  my $o = { opp_id => sprintf('006DEMO%08d', $opp_n), opp_code => $code, %a };
  $o->{opp_link} = "https://example.my.salesforce.com/$o->{opp_id}";
  push @opps, $o;
  $o;
}

for my $acc (@accounts) {
  my ($parent, $team, $region) = @$acc;
  my @children = $region eq q{EU} ? ("$parent UK", "$parent DE") : ("$parent US", ($parent =~ /^[A-M]/ ? "$parent Canada" : ()));
  my $leader = $team ? (grep { $_->[0] eq $team } @teams)[0][1] : undef;
  my $chains = 1 + int(rand(2));
  for my $c (1 .. $chains) {
    my $acct = pick(@children);
    my $market = $acct =~ / UK$/ ? 'UK' : $acct =~ / DE$/ ? 'Germany' : $acct =~ /Canada$/ ? 'Canada' : 'US';
    my $currency = $market eq 'UK' ? 'British Pound' : $market eq 'Germany' ? 'Euro' : $market eq 'Canada' ? 'Canadian Dollar' : 'US Dollars';
    my @cats = do { my %s; grep { !$s{$_}++ } map { pick(@categories) } 1 .. (1 + int(rand(3))) };
    my %prod = map { $_ => { pricing => pricing_for($_), rate => rnd(0.03, 0.10), flat => int(rnd(5, 40)) * 1000,
                             base => rnd(40_000, 1_200_000), minimum => (rand() < 0.2 ? int(rnd(3, 12)) * 1000 : 0) } } @cats;
    $prod{$_}{rate} = rnd(0.01, 0.04) for grep { $prod{$_}{pricing} =~ /Sales|COGS/ } @cats;
    my ($y, $m) = add_months(2023, 6, int(rand(25)));  # first term starts Jun-23 .. Jun-25
    my $term = pick(12, 12, 12, 24);
    my $prev;
    while (1) {
      my ($ey, $em) = add_months($y, $m, $term - 1);
      my $start = ymd($y, $m, 1); my $end = ymd($ey, $em, last_day($ey, $em));
      my $won = $start le '2026-10-01';
      my $o = new_opp(
        opp_name => "$parent - " . ($prev ? 'Renewal' : 'New Business') . " FY" . substr($y, 2), opp_type => $prev ? 'Renewal' : 'New Business',
        opp_stage => $won ? 'Closed Won' : pick('Negotiation', 'Proposal', 'Final Approval'),
        opp_forecast_category => $won ? 'Closed' : pick('Commit', 'Commit', 'Best Case'),
        opp_close_date => ymd(add_months($y, $m, -1), 15), opp_effective_date => $start, opp_rev_start_date => $start,
        opp_contract_end_date => $end, opp_days_of_revenue => $term == 24 ? 730 : 365,
        acc_name => $acct, parent_account => $parent, team => $team, team_leader => $leader,
        opp_owner_name => pick(@owners), opp_owner_team => $region eq q{EU} ? q{EU Sales} : 'NA Sales',
        opp_team_bucket => $prev ? 'Account Management' : 'New Business', opp_currency => $currency,
        opp_previous_opp_code => $prev ? $prev->{opp_code} : undef, market => $market,
      );
      $o->{opp_probability} = $won ? 100 : $o->{opp_forecast_category} eq 'Commit' ? pick(90, 95) : pick(65, 70, 80);
      if ($prev) { $prev->{opp_next_opp_code} = $o->{opp_code}; $prev->{opp_next_opp_stage} = $o->{opp_stage}; $prev->{opp_next_opp_id} = $o->{opp_id} }
      my $deal = 0;
      for my $cat (@cats) {
        my $p = $prod{$cat};
        $p->{base} *= rnd(0.95, 1.25) if $prev;   # spend grows at renewal
        my $pid = sprintf 'PID-%08d', ++$pid_n;
        my $dynamic = $p->{pricing} =~ /%/;
        my $annual = $dynamic ? $p->{base} * $p->{rate} * 12 : $p->{flat} * ($p->{pricing} eq 'One-Off' ? 1 : 12);
        $deal += $annual;
        my @plats = @{ $platforms{$cat} };
        @plats = @plats[0 .. int(rand(@plats))];
        my @mkts = ($market);
        push @mkts, 'Canada' if $market eq 'US' && $cat =~ /Amazon/ && rand() < 0.25;
        for my $pl (@plats) {
          for my $mk (@mkts) {
            push @lines, { %$o, product_id => $pid, product_id => $pid, product_category => $cat, product_name => $product_name{$cat},
                           pricing_structure => $p->{pricing}, platform => $pl, market => $mk, annual => $annual };
          }
        }
        # Contract database: won PIDs and open auto-renewals (not new deals, those are pipeline)
        next unless $won || $prev;
        my $row = ++$row_n;
        push @contract, {
          row_id => $row, contract_item_id => $pid, opportunity_id => $o->{opp_code}, opportunity_stage => $o->{opp_stage},
          opportunity_name => $o->{opp_name}, revenue_type => $prev ? 'Renewal' : 'New', client_type => $prev ? 'Existing' : 'New',
          salesforce_parent_company => $parent, salesforce_client => $acct, product => $product_name{$cat}, product_category => $cat,
          media_platform => join(' / ', @plats), geo => $region, team => $team // 'Unassigned', country => $market,
          rate_type => $dynamic ? 'Dynamic' : 'Flat', dynamic_rate_source => $dynamic ? ($p->{pricing} =~ /Media/ ? 'Media Spend' : 'Sales') : undef,
          pricing_structure => $p->{pricing}, commission_rate => $dynamic ? sprintf('%.4f', $p->{rate}) : undef,
          flat_rate => $dynamic ? undef : $p->{flat}, monthly_minimum => $p->{minimum} || undef, billing_currency => $currency,
          sf_start_date => $start, sf_end_date => $end, forecast_start_date => $start, forecast_termination_date => undef,
          contract_length => $term, previous_opportunity_opp_id => $prev ? $prev->{opp_code} : undef, renewal_assumption => 'Auto-renew',
          _opp => $o,
        };
        for my $mo (@months) {
          my ($yy, $mm) = @$mo; my $mstart = ymd($yy, $mm, 1);
          my $active = ($mstart ge $start && $mstart le $end) ? 1 : 0;
          my $base = $dynamic && $active ? $p->{base} * season($mm) * rnd(0.85, 1.15) : 0;
          my $rev = !$active ? 0 : $dynamic ? $base * $p->{rate} : $p->{pricing} eq 'One-Off' ? ($mstart eq $start ? $p->{flat} : 0) : $p->{flat};
          $rev = $p->{minimum} if $active && $p->{minimum} && $rev < $p->{minimum};
          push @monthly, [$row, $pid, $mstart, sprintf('%.2f', $base), $active, sprintf('%.2f', $rev)];
        }
      }
      $o->{opp_deal_amount} = sprintf '%.2f', $deal;
      $o->{opp_incremental_bookings_amount} = sprintf '%.2f', $prev ? $deal * 0.15 : $deal;
      last unless $won;                 # an open renewal ends the chain
      last if rand() < 0.12;            # churned: no renewal
      last if $end ge '2027-12-31';
      $prev = $o; ($y, $m) = add_months($ey, $em, 1);
    }
  }
}

# Open new-business pipeline (not in the contract database)
for my $i (1 .. 30) {
  my $acc = pick(@accounts[3 .. $#accounts]);
  my ($parent, $team, $region) = @$acc;
  my ($y, $m) = add_months(2026, 11, int(rand(10)));
  my $fc = pick('Pipeline', 'Pipeline', 'Best Case', 'Best Case', 'Commit');
  my $o = new_opp(
    opp_name => "$parent - Expansion " . pick('Walmart', 'DSP', 'Omnichannel', 'Retail'), opp_type => 'New Business',
    opp_stage => pick('Discovery', 'Qualification & Solution Design', 'Proposal', 'Negotiation'), opp_forecast_category => $fc,
    opp_probability => $fc eq 'Commit' ? 90 : $fc eq 'Best Case' ? pick(50, 60, 70, 75) : pick(10, 20, 30),
    opp_close_date => ymd(add_months($y, $m, -1), 20), opp_effective_date => ymd($y, $m, pick(1, 1, 10, 20)),
    opp_rev_start_date => ymd($y, $m, 1), opp_days_of_revenue => pick(180, 365, 365, 365),
    acc_name => $parent . ($region eq q{EU} ? ' UK' : ' US'), parent_account => $parent, team => $team,
    team_leader => $team ? (grep { $_->[0] eq $team } @teams)[0][1] : undef, opp_owner_name => pick(@owners),
    opp_owner_team => $region eq q{EU} ? q{EU Sales} : 'NA Sales', opp_team_bucket => 'New Business', opp_currency => 'US Dollars',
  );
  $o->{opp_contract_end_date} = ymd(add_months($y, $m, 11), 28);
  $o->{opp_incremental_bookings_amount} = $o->{opp_deal_amount} = int(rnd(20, 600)) * 1000;
}

# --- Deliberate data issues, so the checks have something to find -------------------------------------
my @won_lines = grep { $_->{opp_stage} eq 'Closed Won' && $_->{product_category} !~ /Consultancy|Content/ && $_->{opp_contract_end_date} ge '2026-12-31' } @lines;
my %drop = map { $_->{product_id} => 1 } @won_lines[-6 .. -1];   # 6 recently won PIDs not yet in the contract DB -> rev_check_new_pids
@contract = grep { !$drop{ $_->{contract_item_id} } } @contract;
my %kept = map { $_->{row_id} => 1 } @contract;
@monthly = grep { $kept{ $_->[0] } } @monthly;
my @dyn = grep { $_->{rate_type} eq 'Dynamic' && $_->{sf_end_date} ge '2026-06-30' } @contract;
$dyn[3]{team} = $dyn[3]{team} eq q{NA-South} ? q{NA-East} : q{NA-South};          # team differs from Salesforce -> rev_check_salesforce
$dyn[8]{sf_end_date} = '2026-12-15';                           # end date differs
$dyn[12]{opportunity_stage} = q{Closed Lost};                     # stage differs
push @contract, { %{ $dyn[15] }, row_id => ++$row_n, contract_item_id => 'PID-09999999', opportunity_name => $dyn[15]{opportunity_name} . ' (manual)' };  # PID not in Salesforce
push @monthly, map { [$row_n, "PID-09999999", @$_[2 .. 5]] } grep { $_->[0] == $dyn[15]{row_id} } @monthly;
$_->{market} = '' for grep { $_->{product_id} eq $won_lines[0]{product_id} } @lines;              # blank market -> warning
$_->{market} = 'Mexico' for grep { $_->{product_id} eq $won_lines[10]{product_id} } @lines;       # market outside US/Canada -> warning

# --- Write ---------------------------------------------------------------------------------------------
my @opp_cols = qw(opp_id opp_code opp_name opp_type opp_stage opp_forecast_category opp_probability opp_close_date opp_effective_date
  opp_rev_start_date opp_contract_end_date opp_days_of_revenue opp_deal_amount opp_incremental_bookings_amount opp_currency acc_name
  parent_account team team_leader opp_owner_name opp_owner_team opp_team_bucket opp_previous_opp_code
  opp_next_opp_code opp_next_opp_stage opp_next_opp_id opp_link);
open my $fo, '>', "$out/sf_opportunity.csv" or die $!;
print $fo csv(@opp_cols);
for my $o (@opps) { print $fo csv(map { $o->{$_} } @opp_cols) }
close $fo;

my @line_cols = qw(product_id platform market pricing_structure product_category product_name opp_id opp_code
  opp_name opp_type opp_stage opp_probability opp_forecast_category opp_close_date opp_effective_date opp_contract_end_date opp_currency
  opp_deal_amount product_amount acc_name parent_account team opp_owner_name opp_owner_team
  opp_next_opp_id opp_link);
my %per_opp; $per_opp{ $_->{opp_code} }{ $_->{product_id} } = 1 for @lines;
open my $fl, '>', "$out/sf_opportunity_products.csv" or die $!;
print $fl csv(@line_cols);
for my $l (@lines) {
  my $o = (grep { $_->{opp_code} eq $l->{opp_code} } @opps)[0];
  $l->{$_} = $o->{$_} for qw(opp_deal_amount opp_next_opp_id);
  $l->{product_amount} = sprintf '%.2f', $o->{opp_deal_amount} / keys %{ $per_opp{ $l->{opp_code} } };
  print $fl csv(map { $l->{$_} } @line_cols);
}
close $fl;

my @c_cols = qw(row_id contract_item_id opportunity_id opportunity_stage opportunity_name revenue_type client_type salesforce_parent_company
  salesforce_client product product_category media_platform geo team country rate_type dynamic_rate_source pricing_structure
  commission_rate flat_rate monthly_minimum billing_currency sf_start_date sf_end_date forecast_start_date
  forecast_termination_date contract_length previous_opportunity_opp_id next_renewal_opp_id renewal_assumption);
for my $c (@contract) { $c->{next_renewal_opp_id} = $c->{_opp}{opp_next_opp_code} }
open my $fc, '>', "$out/contract_db.csv" or die $!;
print $fc csv(@c_cols);
for my $c (sort { $a->{row_id} <=> $b->{row_id} } @contract) { print $fc csv(map { $c->{$_} } @c_cols) }
close $fc;

open my $fm, '>', "$out/contract_db_monthly.csv" or die $!;
print $fm csv(qw(row_id contract_item_id month base_amount active_flag revenue));
print $fm csv(@$_) for @monthly;
close $fm;

printf "%d opportunities, %d product lines, %d contract rows, %d monthly rows\n", scalar @opps, scalar @lines, scalar @contract, scalar @monthly;
