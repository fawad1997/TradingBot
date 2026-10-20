/* Quantara - the plain-language layer.
 *
 * The engine speaks in codes: SPREAD_ABOVE_MAX, RISK_STATE_REDUCED, SMALL_WIN_PROFIT_FLOOR. Those are the
 * right names inside the code and in the database, and the logs keep them. But a dashboard exists to be
 * understood at a glance by a person who is not holding the whole system in their head, so every code the
 * screen shows gets a sentence here.
 *
 * Each entry has:
 *   say   - what happened, in words anyone can read. No jargon, no abbreviations.
 *   mean  - why it matters, or what the system is doing about it. Optional.
 *   tone  - good | bad | wait | info. Drives colour, so it must describe the SITUATION, not the severity
 *           of the log line: a gate that refused a bad trade is 'good', not 'bad'.
 *
 * Anything not listed falls through to humanise(), which turns SOME_UNKNOWN_CODE into "Some unknown code"
 * rather than showing the raw token. The raw token is always still available on hover.
 */

const GLOSSARY = {
  // ---------------------------------------------------------------- the system is fine
  SYSTEM_READY:            {say: "Ready and watching", mean: "Every check passed. Waiting for a setup worth taking.", tone: "good"},
  TRADING:                 {say: "Trading", tone: "good"},
  HEALTHY:                 {say: "Healthy", tone: "good"},
  NORMAL:                  {say: "Normal", tone: "good"},
  ACTIVE:                  {say: "Running", tone: "good"},
  ELIGIBLE:                {say: "Allowed to trade", tone: "good"},
  VALIDATED:               {say: "Checked and trusted", tone: "good"},

  // ---------------------------------------------------------------- waiting, not broken
  WAITING:                 {say: "Waiting", tone: "wait"},
  INITIALIZING:            {say: "Starting up", mean: "Loading price history and connecting to the broker.", tone: "wait"},
  STARTUP:                 {say: "Starting up", tone: "wait"},
  NO_SETUP:                {say: "No setup right now", mean: "The chart does not match what this rule looks for. This is the normal state most of the time.", tone: "info"},
  NO_TRIGGER:              {say: "No entry signal yet", mean: "Waiting for the candle pattern this rule needs.", tone: "info"},
  NO_M1_DATA:              {say: "Waiting for 1-minute candles", tone: "wait"},
  NO_M1_HISTORY:           {say: "Waiting for enough price history", tone: "wait"},
  NO_TRIGGER_TIMEFRAME_DATA: {say: "Waiting for candles on the timeframe this rule uses", tone: "wait"},
  INSUFFICIENT_HISTORY:    {say: "Not enough price history yet", mean: "It needs more past candles before it can judge anything.", tone: "wait"},
  ATR_NOT_READY:           {say: "Still measuring how much this market moves", tone: "wait"},
  ATR_UNAVAILABLE:         {say: "Cannot measure how much this market moves", mean: "Without that, it cannot size a sensible stop.", tone: "wait"},
  MARKET_CLOSED:           {say: "Market closed", mean: "The broker is not quoting prices for this symbol.", tone: "info"},
  DUPLICATE_SETUP:         {say: "Already acted on this setup", mean: "The same signal fired twice; it was not taken twice.", tone: "info"},
  DEDUPLICATED:            {say: "Same setup as before, skipped", tone: "info"},

  // ---------------------------------------------------------------- cost and spread
  SPREAD:                  {say: "Buy/sell gap too wide", tone: "good"},
  SPREAD_ABOVE_MAX:        {say: "Buy/sell gap too wide", mean: "The gap between the buy and sell price is above the limit for this symbol, so the trade would start too far behind.", tone: "good"},
  SPREAD_TOO_LARGE_VS_STOP:{say: "The broker's gap would eat the trade", mean: "The buy/sell gap is large compared with the stop distance. Trades like this lose money on costs alone, whatever the chart says.", tone: "good"},
  SPREAD_PERCENTILE:       {say: "Buy/sell gap unusually wide for this time of day", tone: "good"},
  SPREAD_SHOCK:            {say: "The buy/sell gap jumped suddenly", mean: "Usually means news just hit or liquidity vanished. It waits for things to settle.", tone: "wait"},
  SPREAD_NOT_WITHIN_SAFE_RANGE: {say: "Price gap outside the safe range", tone: "good"},
  SPREAD_AT_EXECUTION:     {say: "Gap widened at the moment of sending", mean: "It refused to send rather than pay the wider cost.", tone: "good"},
  COST:                    {say: "Not worth the costs", tone: "good"},
  EDGE:                    {say: "Expected gain too small to cover costs", tone: "good"},
  REWARD_TOO_SMALL:        {say: "Target too close to be worth it", tone: "good"},

  // ---------------------------------------------------------------- risk and money
  RISK:                    {say: "Risk limit", tone: "wait"},
  RISK_BUDGET_EXHAUSTED:   {say: "No risk budget left", mean: "Open positions already use the most this account is allowed to risk at once.", tone: "wait"},
  RISK_RESERVATION_REJECTED: {say: "Would exceed the total risk limit", tone: "wait"},
  REDUCED:                 {say: "Trading at half size", mean: "The account is down from its peak, so each trade risks less until it recovers.", tone: "wait"},
  RESTRICTED:              {say: "Trading at quarter size", mean: "The account is further down from its peak. Sizes are cut hard.", tone: "wait"},
  NO_NEW_TRADES:           {say: "No new trades today", mean: "A loss limit was reached. Open positions are still managed normally.", tone: "bad"},
  EMERGENCY:               {say: "Stopped — emergency drawdown", mean: "The account fell far enough that everything new is halted.", tone: "bad"},
  SYMBOL_LOSS_COOLDOWN:    {say: "Cooling off after losses here", mean: "Several losses in a row on this symbol, so it is standing down for a while.", tone: "wait"},
  MAX_CONCURRENT_POSITIONS:{say: "Already holding the maximum number of positions", tone: "wait"},
  SYMBOL_ALREADY_EXPOSED:  {say: "Already in a trade on this symbol", tone: "info"},
  PORTFOLIO:               {say: "Portfolio limit", tone: "wait"},
  SIZE:                    {say: "Position size would not work", tone: "wait"},
  SIZE_INFEASIBLE_AT_EXECUTION: {say: "Could not size the trade", mean: "The smallest allowed trade would risk more than the rules permit.", tone: "wait"},
  MARGIN:                  {say: "Not enough free margin", tone: "wait"},
  MARGIN_LEVEL_LOW:        {say: "Margin level too low to add risk", tone: "bad"},
  HIGH_VOLATILITY:         {say: "Market moving unusually fast", mean: "Sizes are reduced while this lasts.", tone: "wait"},
  DRAWDOWN_REDUCED:        {say: "Sizes cut because the account is down from its best", tone: "wait"},
  DRAWDOWN_RESTRICTED:     {say: "Sizes cut hard — the account is well down from its best", tone: "wait"},
  DRAWDOWN_RECOVERED:      {say: "Back to full size — the account recovered", tone: "good"},
  DAILY_LOSS_LIMIT:        {say: "Hit the most it is allowed to lose in one day", tone: "bad"},

  // ---------------------------------------------------------------- stopped on purpose
  KILL_SWITCH:             {say: "Emergency stop is on", mean: "No new trades. Positions already open keep their stops at the broker.", tone: "bad"},
  KILL_SWITCH_ENGAGED:     {say: "Emergency stop is on", tone: "bad"},
  HALTED:                  {say: "Halted", tone: "bad"},
  STOPPED:                 {say: "Stopped", tone: "bad"},
  DISABLED:                {say: "Switched off", tone: "info"},
  DISABLED_IN_CONFIG:      {say: "Switched off in the settings file", tone: "info"},
  SYMBOL_TRADING_STOPPED:  {say: "You stopped this symbol", mean: "No new trades here until you start it again.", tone: "info"},
  SYMBOL_NOT_IN_STRADDLE_LIST: {say: "News trading not set up for this symbol", tone: "info"},
  SYMBOL_DISABLED:         {say: "This symbol is stopped", tone: "info"},
  STRATEGY_STATUS:         {say: "This rule is not cleared to trade", tone: "info"},
  SHADOW_MODE:             {say: "Watching only, not trading", mean: "It records what it would have done so you can judge it without risking money.", tone: "info"},
  RESEARCH_ONLY:           {say: "For research only", tone: "info"},

  // ---------------------------------------------------------------- news and timing
  EVENT:                   {say: "Major news due", mean: "It stands aside around high-impact releases, when prices jump unpredictably.", tone: "good"},
  TOO_CLOSE_TO_RELEASE:    {say: "Too close to the news release", tone: "good"},
  EVENT_CALENDAR_MISSING_OR_STALE: {say: "News calendar is out of date", mean: "It cannot tell when the next big release is, so it stays cautious.", tone: "wait"},
  SESSION:                 {say: "Outside this rule's trading hours", tone: "info"},
  STALE:                   {say: "Signal went stale", tone: "info"},
  STALE_SIGNAL:            {say: "Signal went stale before it could be sent", mean: "Price moved on while the checks were running.", tone: "info"},
  ENTRY_DRIFT:             {say: "Price moved too far from the planned entry", tone: "good"},

  // ---------------------------------------------------------------- the research gate and the filter
  RESEARCH:                {say: "Backtests found no edge here", mean: "The research lab tested this rule on this symbol's history and it did not make money.", tone: "good"},
  ML_FILTER:               {say: "The learned filter scored this setup too low", mean: "A model trained on past trades judged this one unlikely to work.", tone: "good"},
  NOT_VALIDATED:           {say: "Not trusted yet", mean: "It is scoring trades but not blocking any, until it proves itself on unseen data.", tone: "info"},

  // ---------------------------------------------------------------- connection and plumbing
  HEALTH:                  {say: "A system check failed", tone: "bad"},
  DOWN:                    {say: "Not working", tone: "bad"},
  DEGRADED:                {say: "Working, but not well", tone: "wait"},
  WATCH:                   {say: "Worth keeping an eye on", tone: "wait"},
  UNKNOWN:                 {say: "Cannot tell", tone: "wait"},
  BROKER_TRADING_DISABLED: {say: "The broker is not accepting trades", mean: "Check that AutoTrading is on in MetaTrader 5.", tone: "bad"},
  BROKER_POSITIONS_UNKNOWN:{say: "Cannot read positions from the broker", mean: "It will not open anything new until it can see what is already open.", tone: "bad"},
  ACCOUNT_STATE_UNAVAILABLE: {say: "Cannot read the account balance", tone: "bad"},
  ACCOUNT_OR_POSITIONS_UNAVAILABLE: {say: "Cannot read the account or its positions", tone: "bad"},
  DATABASE_WRITE_FAILED:   {say: "Could not save to the database", tone: "bad"},
  REPEATED_LOOP_ERRORS:    {say: "Something keeps going wrong internally", mean: "New trades are halted until it is looked at.", tone: "bad"},
  THROTTLED:               {say: "The broker asked it to slow down", mean: "Normal under load. It retries.", tone: "wait"},
  OWN_COOLDOWN:            {say: "Held back by our own spacing rule", mean: "The bot limits how often it moves a stop, so one small nudge cannot crowd out the move that matters. Nothing is wrong, and it retries.", tone: "info"},
  CROSSED_QUOTE:           {say: "Broker price feed looks wrong", tone: "bad"},
  RECONCILIATION_REQUIRED: {say: "Needs checking against the broker", mean: "Its record and the broker's do not agree yet.", tone: "wait"},
  RECONCILED:              {say: "Matched up with the broker", tone: "good"},
  BROKER_STATE_DIFFERS:    {say: "The broker shows something different", tone: "wait"},
  EXECUTION:               {say: "The order could not be placed", tone: "bad"},

  // ---------------------------------------------------------------- what happened to a trade
  FILLED:                  {say: "Opened", tone: "good"},
  OPEN:                    {say: "Open", tone: "good"},
  CLOSED:                  {say: "Closed", tone: "info"},
  BROKER_CLOSED:           {say: "Closed at the broker", tone: "info"},
  CANCELLED:               {say: "Cancelled", tone: "info"},
  EXPIRED:                 {say: "Expired before it filled", tone: "info"},
  REJECTED:                {say: "Refused", tone: "info"},
  APPROVED:                {say: "Approved, sending", tone: "wait"},
  ORDER_SENT:              {say: "Order sent, waiting for fill", tone: "wait"},
  PROTECTION:              {say: "Protected", mean: "The stop has been moved so this trade can no longer lose.", tone: "good"},
  PARTIAL:                 {say: "Part closed", tone: "good"},
  RUNNER:                  {say: "Letting the rest run", tone: "good"},
  USER_MODIFIED:           {say: "You changed this one by hand", tone: "info"},
  SL:                      {say: "Stop loss hit", mean: "The stop order filled. That can still be a profit if the stop had been moved up.", tone: "info"},
  TP:                      {say: "Target reached", tone: "good"},
  STOP_OUT:                {say: "Closed by the broker — margin", tone: "bad"},
  TARGET_REACHED:          {say: "Target reached", tone: "good"},
  SMALL_WIN_PROFIT_FLOOR:  {say: "Locked in a small profit", mean: "It was up enough to be worth protecting, so the stop was moved to keep most of it.", tone: "good"},
  AMBITION_PROFIT_FLOOR:   {say: "Locked in a bigger profit", tone: "good"},
  DYNAMIC_PROFIT_FLOOR:    {say: "Trailing the profit", mean: "The stop follows the price up, giving back only a set share of the best level reached.", tone: "good"},
  HARD_PROFIT_TARGET:      {say: "Closed at the fixed profit target", tone: "good"},
  BREAKEVEN_AFTER_PARTIAL: {say: "Moved to break even after taking part off", tone: "good"},
  SL_MOVED:                {say: "Stop moved", tone: "info"},
  SL_TOO_CLOSE:            {say: "The broker refused the stop — too close to price", mean: "The position keeps its existing stop and it tries again shortly.", tone: "wait"},
  REMOVED_EXTERNALLY:      {say: "Closed outside the bot", mean: "Someone or something else closed it in MetaTrader.", tone: "info"},
  UNEXPECTED_POSITION_ADOPTED: {say: "Found a position it did not open", mean: "It has taken over managing it.", tone: "wait"},

  // ---------------------------------------------------------------- news straddle
  ARMED:                   {say: "Orders placed, waiting for the news", tone: "wait"},
  ARMED_ONE_SIDED:         {say: "Only one side could be placed", tone: "wait"},
  ONE_FILLED:              {say: "One side triggered", mean: "The other order was cancelled automatically.", tone: "good"},
  BOTH_FILLED:             {say: "Both sides triggered", mean: "Price whipsawed through both. This is the bad case for a straddle.", tone: "bad"},
  IDLE:                    {say: "Nothing scheduled", tone: "info"},
  MAX_ARMED_STRADDLES:     {say: "Already holding news orders on the maximum number of symbols", tone: "wait"},
  SPREAD_SHOCK_BEFORE_RELEASE: {say: "Stood down — the price gap blew out before the news", tone: "good"},
  STRADDLE_PARTNER_FAILED: {say: "Cancelled — the other side could not be placed", mean: "It never leaves a one-sided news bet on purpose.", tone: "good"},

  // ---------------------------------------------------------------- strategy reasons
  H1_NOT_RANGE:            {say: "The hourly chart is trending, and this rule only trades ranges", tone: "info"},
  H1_TREND_INVALID:        {say: "No clear hourly trend to follow", tone: "info"},
  H1_HISTORY_UNAVAILABLE:  {say: "Hourly candles not available yet", tone: "wait"},
  H1_FEATURES_NAN:         {say: "Hourly measurements could not be calculated", tone: "wait"},
  H4_MACRO_CHANNEL_UNAVAILABLE: {say: "The 4-hour trend channel is not ready", tone: "wait"},
  IMPULSE_TOO_SMALL:       {say: "The move was too weak to follow", tone: "info"},
  VOLATILITY_EXPANSION:    {say: "Movement is picking up", tone: "info"},
  BULLISH_PIN_BAR:         {say: "Buy signal — rejection candle", tone: "info"},
  BEARISH_PIN_BAR:         {say: "Sell signal — rejection candle", tone: "info"},
  BULLISH_ENGULFING:       {say: "Buy signal — engulfing candle", tone: "info"},
  BEARISH_ENGULFING:       {say: "Sell signal — engulfing candle", tone: "info"},
  // ---------------------------------------------------------------- health blockers (app/governance/health.py)
  // These are the exact strings that stop trading and appear on the status panel, so every one of them is
  // written out: a blocker you cannot read is a blocker you cannot act on.
  BROKER_DISCONNECTED:     {say: "Lost the connection to MetaTrader", mean: "No new trades until it is back. Open positions keep their stop and target at the broker.", tone: "bad"},
  STALE_DATA:              {say: "Prices have stopped arriving", mean: "The feed has gone quiet for longer than it should. It will not trade on a price it cannot trust.", tone: "bad"},
  ABNORMAL_TICKS:          {say: "The price feed looks unreliable", mean: "Several odd prices arrived recently, so it is being cautious.", tone: "wait"},
  DATABASE_DOWN:           {say: "The database is unreachable", mean: "Trading stops: the bot will not trade what it cannot record.", tone: "bad"},
  EXECUTION_FAILURE_CLUSTER: {say: "Several orders failed in a row", mean: "Something is wrong between the bot and the broker. New trades are paused.", tone: "bad"},
  PORTFOLIO_STATE_UNCERTAIN: {say: "Not sure what is currently open", mean: "It will not add risk until its record and the broker's agree.", tone: "bad"},
  UNEXPECTED_POSITION:     {say: "Found a position it did not open", mean: "Someone or something else opened a trade on this account.", tone: "wait"},

  // ---------------------------------------------------------------- execution outcomes
  ABORTED:                 {say: "Called off before anything was sent", tone: "info"},
  ABORTED_PLACEMENT_FAILED:{say: "Called off — the order could not be placed", tone: "wait"},
  ALREADY_CLOSED_UNCONFIRMED: {say: "Looks closed, but the broker has not confirmed", tone: "wait"},
  AMBIGUOUS_CLOSE:         {say: "Unclear whether the close went through", mean: "It checks again with the broker rather than guessing.", tone: "wait"},
  AMBIGUOUS_ORDER_RESULT:  {say: "Unclear whether the order went through", tone: "wait"},
  AMBIGUOUS_SL_CHANGE:     {say: "Unclear whether the stop was moved", tone: "wait"},
  ACCOUNT_EQUITY_UNAVAILABLE: {say: "Cannot read what the account is worth", tone: "bad"},
  CONFIRMED_NOT_FILLED:    {say: "Confirmed: the order did not fill", tone: "info"},
  FILL_NOT_VISIBLE:        {say: "The fill has not shown up at the broker yet", tone: "wait"},
  CLOSE_DEAL_NOT_VISIBLE:  {say: "The closing deal has not shown up yet", tone: "wait"},
  ATTACH_SL:               {say: "Attaching the stop", tone: "info"},
  SL_CHANGE:               {say: "Moving the stop", tone: "info"},
  CLOSE_REQUEST:           {say: "Asking the broker to close", tone: "info"},
  CLOSE_UNPROTECTED:       {say: "Closed because it had no stop", mean: "A position without a stop is closed rather than left exposed.", tone: "wait"},
  UNPROTECTED_CLOSE_REQUESTED: {say: "Closing a position that has no stop", tone: "wait"},
  REVALIDATED_RETRY:       {say: "Re-checked and tried again", tone: "info"},
  ENTRY_DRIFT:             {say: "Price moved too far from the planned entry", tone: "good"},
  STOP_INVALID:            {say: "The broker rejected the stop price", tone: "wait"},
  STOP_INVALID_AT_PRICE:   {say: "The stop was not valid at that price", tone: "wait"},
  INVALID_STOP_SIDE:       {say: "The stop was on the wrong side of the price", tone: "bad"},
  TARGET_ALREADY_REACHED:  {say: "Price was already past the target", tone: "info"},
  EVALUATION_ERROR:        {say: "The rule could not finish its calculation", tone: "wait"},
  STRATEGY_OR_ROUTE_UNAVAILABLE: {say: "That rule is not available right now", tone: "wait"},
  CANDLESTICK_TRIGGERS_DISABLED: {say: "Candle patterns are switched off for this rule", tone: "info"},
  INVALID_STOP_OR_EQUITY:  {say: "Could not work out the size — bad stop or balance", tone: "wait"},
  ALREADY_RESUMED:         {say: "It was already running", tone: "info"},
  CORRUPT:                 {say: "A saved record could not be read", tone: "bad"},
  UNCERTAIN:               {say: "Not certain", tone: "wait"},
  SKIPPED:                 {say: "Skipped", tone: "info"},
  FINISHED:                {say: "Finished", tone: "info"},
  EXECUTED:                {say: "Done", tone: "good"},
  ROUTING:                 {say: "Deciding how to send it", tone: "info"},
  CREATED:                 {say: "Created", tone: "info"},
};

/* Names for the pipeline stages, used by the "why it didn't trade more" funnel. */
const GATE_GROUPS = {
  STRATEGY:    "The chart did not match the rule",
  COST:        "Not worth the trading costs",
  SPREAD:      "Buy/sell gap too wide",
  EDGE:        "Expected gain too small",
  RISK:        "Risk limits",
  PORTFOLIO:   "Risk limits",
  SIZE:        "Risk limits",
  MARGIN:      "Risk limits",
  HEALTH:      "A system check failed",
  EXECUTION:   "The order could not be placed",
  KILL_SWITCH: "Emergency stop",
  EVENT:       "Major news due",
  SESSION:     "Outside trading hours",
  REGIME:      "Wrong market conditions for this rule",
  RESEARCH:    "Backtests found no edge",
  ML_FILTER:   "The learned filter said no",
  STALE:       "Signal went stale",
  SYMBOL_DISABLED: "You stopped this symbol",
  STRATEGY_STATUS: "Rule not cleared to trade",
  VISION:      "Retired chart-grading step",
};

/* SOME_CODE_LIKE_THIS -> "Some code like this". The fallback when nothing is written above. */
function humanise(code) {
  const s = String(code ?? "").trim();
  if (!s) return "–";
  if (!/^[A-Z0-9_]+$/.test(s)) return s;                       // already prose, leave it
  const words = s.toLowerCase().replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/* The entry for a code, always returning something usable. */
function term(code) {
  const key = String(code ?? "").trim().toUpperCase();
  if (GLOSSARY[key]) return {...GLOSSARY[key], code: key};
  // reasons often arrive as "CODE (extra detail)" or "CODE: detail" - match the leading code
  const head = key.split(/[\s(:]/)[0];
  if (GLOSSARY[head]) return {...GLOSSARY[head], code: key};
  for (const pre of ["RISK_STATE_", "STRADDLE_LEG_", "FILLED_"]) {
    if (key.startsWith(pre) && GLOSSARY[key.slice(pre.length)]) return {...GLOSSARY[key.slice(pre.length)], code: key};
  }
  return {say: humanise(key), tone: "info", code: key};
}

/* Just the sentence. */
const say = code => term(code).say;

/* The sentence plus the "why it matters" line, for places with room for both. */
function explain(code) {
  const t = term(code);
  return t.mean ? `${t.say} — ${t.mean}` : t.say;
}
