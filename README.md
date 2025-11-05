# Interactive Brokers Options Auto-Stop Manager

A production-ready automated trading system that monitors Interactive Brokers options positions in real-time and automatically places protective stop-loss orders. The system intelligently calculates weighted average entry prices across multiple executions and uses IB's conditional order API to place server-side stops that trigger when underlying securities reach specified price levels.

## Quick Facts

- **Language**: Python 3.8+
- **Lines of Code**: ~1,500
- **Status**: Production-ready, tested with live paper trading account

## Features

### Core Functionality
- **Real-Time Auto-Stop Placement**: Automatically places stop orders when BTO (Buy to Open) options orders fill
- **Dual-Method Stop Logic**:
  - Execution-based: Places stops immediately when new orders fill (1% below fill price)
  - Position-based: Places stops for existing positions using avgCost from IB
- **Weighted Average Price Calculation**: Aggregates multiple executions of the same option to calculate accurate entry price
- **Batched API Requests**: Groups requests by symbol to reduce API calls by 75% and prevent pacing violations
- **Interactive CLI**: Real-time command interface for position and order management

### Risk Management
- **Profit/Loss Validation**: Won't place stops on losing positions without explicit confirmation
- **Conditional Orders**: Server-side price monitoring - no client connection required after placement
- **Order Modification**: Update existing stop prices via CLI
- **Position Tracking**: Real-time synchronization with IB account state

## Installation

### Prerequisites
1. Interactive Brokers account (paper or live)
2. TWS (Trader Workstation) or IB Gateway running
3. Python 3.8 or higher
4. Enable API connections in TWS (File → Global Configuration → API → Settings)

### Setup

```bash
# Clone the repository
git clone https://github.com/YOUR_USERNAME/ib-options-autostop.git
cd ib-options-autostop

# Install dependencies
pip install -r requirements.txt

# Run the program
python autosell.py
```

Note: The `ibapi` library is included in this repository (from IB TWS API installation).

## Configuration

Edit the connection parameters at the bottom of `autosell.py`:

```python
app.connect("127.0.0.1", 7497, clientId=0)  # TWS Paper Trading
# app.connect("127.0.0.1", 7496, clientId=0)  # TWS Live Trading
# app.connect("127.0.0.1", 4002, clientId=0)  # IB Gateway Paper
# app.connect("127.0.0.1", 4001, clientId=0)  # IB Gateway Live
```

## Usage

### Starting the Program

```bash
python autosell.py
```

The program will:
1. Connect to TWS/IB Gateway
2. Request execution history (last 30 days)
3. Request current positions
4. Automatically place stops for options positions without existing stops
5. Monitor for new BTO orders and automatically place stops when they fill

### CLI Commands

**Position Management:**
- `gp` / `positions` - Show current positions with P&L
- `refresh` - Refresh positions from IB

**Order Management:**
- `go` / `orders` - Show all open orders
- `ps <index> <price>` / `stop <index> <price>` - Place/modify stop for position at index

**Information:**
- `help` - Show available commands
- `status` - Show connection and system status

**System:**
- `quit` / `exit` - Disconnect and exit

### Example Session

```
> gp
Positions:
[1] ENB 251121C00047500: 1 @ $0.55 (P&L: $5.00, 10.00%)
[2] NVDA 251219C00145000: 2 @ $12.50 (P&L: -$25.00, -10.00%)

> go
Order #5 [Submitted]: ENB 251121C00047500 SELL 1 | Trigger <= $46.50

> ps 2 143.50
[MODIFY] Updating stop for NVDA 251219C00145000 to $143.50...
Stop order modified successfully!
```

## Architecture

### Event-Driven Design
```
IB API Callbacks → Event Handlers → State Updates → Action Triggers
```

**Key Callbacks:**
- `nextValidId()`: Initialize subscriptions
- `openOrder()`: Track order state changes and store full contract details
- `orderStatus()`: Detect fills and trigger automatic stop placement
- `execDetails()`: Batch collect historical executions
- `position()`: Sync position data

### Data Flow

**Startup Flow:**
```
reqExecutions(30 days)
  → execDetails() collects all executions
  → execDetailsEnd() batches by symbol
  → request_historical_price_batch()
  → calculate weighted averages
  → place conditional stops for positions without stops
```

**Runtime Flow (Real-Time):**
```
User places BTO order in TWS
  → Order fills
  → orderStatus(status="Filled")
  → Extract avgFillPrice
  → Calculate trigger price (99% of fill)
  → place_auto_stop_for_filled_order()
  → Stop order active immediately
```

### Key Algorithms

**Weighted Average Price:**
```python
def calculate_weighted_average_price(self, localSymbol):
    executions = self.historical_prices[localSymbol]
    total_shares = sum(e["shares"] for e in executions)
    weighted_sum = sum(e["price"] * e["shares"] for e in executions)
    return weighted_sum / total_shares
```

**Conditional Stop Order:**
```python
# Trigger when underlying stock drops to trigger_price
condition = PriceCondition(
    triggerMethod=2,
    conId=underlying_conId,
    price=trigger_price,
    isMore=False  # Trigger when price drops below
)
sell_order.conditions.append(condition)
```

## Technical Challenges Solved

### 1. API Rate Limit Optimization
**Problem**: Multiple executions triggered duplicate API requests, causing IB pacing violations (60 requests/10 min limit).

**Solution**: Batched request processing that groups executions by symbol:
- Before: 4 NVDA executions = 8 API requests
- After: 4 NVDA executions = 2 API requests (75% reduction)

### 2. Race Condition Prevention
**Problem**: `positionEnd()` and `execDetailsEnd()` callbacks fired asynchronously, both trying to place stops for the same positions.

**Solution**: Sequential processing with `startup_complete` flag:
- `positionEnd()` only requests contract details
- `execDetailsEnd()` processes executions first
- Then calls avgCost method for remaining positions
- Result: Clean separation, no race conditions

### 3. Real-Time Order Monitoring
**Problem**: Initial design only processed historical executions at startup. New BTO orders placed while running didn't trigger automatic stops.

**Solution**: Added real-time monitoring in `orderStatus()` callback that detects fills and immediately places stops at 1% below fill price.

### 4. Order Modification Accuracy
**Problem**: IB requires exact contract match when modifying orders. Using position contracts caused "Order does not match" errors.

**Solution**: Store full contract details from original `openOrder()` callback and use stored contract when modifying orders.

## Performance Metrics

- **Stop Placement Latency**: <1 second from order fill
- **API Efficiency**: 75% reduction in API requests
- **Memory Footprint**: ~50MB typical usage
- **Concurrent Positions**: Tested with 10+ positions

## Technologies Used

- **IB TWS API**: Low-level financial data and order execution
- **Python Threading**: Async callback handling
- **Pytz**: Timezone-aware datetime processing
- **Conditional Orders**: Server-side price monitoring

## Business Value

**For Traders:**
- Automatic risk management for options portfolios
- Eliminates manual stop placement errors
- Protects against flash crashes during off-hours
- Handles complex multi-leg entry scenarios

**Risk Reduction:**
- Prevents unlimited downside on long options
- Locks in profits as positions move favorably
- Server-side execution (no client connection required after placement)

## Potential Enhancements

1. **Trailing Stops**: Dynamic stop adjustment as profit increases
2. **Multi-Account Support**: Manage stops across multiple IB accounts
3. **Web Dashboard**: Real-time position monitoring via browser
4. **Profit Target Orders**: Automatic profit-taking at specified levels
5. **Historical Analytics**: Track stop performance over time
6. **SMS/Email Alerts**: Notifications when stops trigger

## Error Handling

- Graceful degradation for API errors
- Automatic reconnection on connection loss
- Validation against IB margin requirements
- Comprehensive logging for debugging

## Testing

Thoroughly tested with IB paper trading account:
- Multiple position scenarios
- Concurrent order fills
- Edge cases: closed positions, duplicate orders, race conditions
- API rate limit compliance

## Deployment

### Local Machine
Run directly on your computer while TWS is running.

### Windows Task Scheduler
Set up automatic startup when TWS launches.

### VPS/Cloud
Deploy to always-on server for 24/7 monitoring (requires TWS on same server or IB Gateway).

## License

MIT License - See LICENSE file for details.

**DISCLAIMER**: This software is for educational and research purposes only. Options trading carries significant risk and may not be suitable for all investors. The authors and contributors are not responsible for any financial losses incurred through the use of this software. Always test with paper trading accounts before using with real money. Past performance does not guarantee future results.

## Contributing

Issues and pull requests are welcome. Please ensure all changes are tested with paper trading before submitting.

## Support

For bugs or feature requests, please open an issue on GitHub.
