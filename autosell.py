"""
Interactive Brokers Options Auto-Stop Manager

A production-ready automated trading system that monitors Interactive Brokers options
positions in real-time and automatically places protective stop-loss orders.

Author: thral1
License: MIT
"""

import threading
import queue
import time
import datetime
import pytz
import argparse
import copy
from typing import Dict, List, Optional, Tuple, Any
from collections import defaultdict

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.order import Order
from ibapi.order_cancel import OrderCancel
from ibapi.order_state import OrderState
from ibapi.order_condition import PriceCondition
from ibapi.execution import Execution, ExecutionFilter
from ibapi.common import OrderId


class Config:
    """Application configuration constants"""

    # Connection settings
    TWS_HOST = "127.0.0.1"
    TWS_PORT = 4001  # 4001=TWS Live, 4002=Gateway Paper, 7497=TWS Paper
    CLIENT_ID = 0

    # Trading parameters
    STOP_LOSS_PERCENTAGE = 0.01  # 1% stop loss
    EXECUTION_HISTORY_DAYS = 30

    # API rate limiting
    HISTORICAL_DATA_DELAY = 0.5  # seconds between requests
    CONTRACT_DETAIL_DELAY = 0.5
    AVGCOST_RETRY_DELAY = 3

    # Display formatting
    SEPARATOR_WIDTH = 80
    SEPARATOR_SHORT = 60


class PositionManager:
    """Manages position tracking and display"""

    def __init__(self):
        self.positions: Dict[Tuple[str, str], Dict] = {}
        self.display_positions: List[Dict] = []

    def add_or_update(self, account: str, contract: Contract, position: float,
                     avg_cost: float, startup_complete: bool) -> None:
        """Add or update a position"""
        position_info = {
            "account": account,
            "symbol": contract.symbol,
            "localSymbol": contract.localSymbol,
            "secType": contract.secType,
            "position": position,
            "avgCost": avg_cost
        }

        position_key = (account, contract.localSymbol)
        self.positions[position_key] = position_info

        display_info = {
            'right': contract.right,
            'account': account,
            'symbol': contract.symbol,
            'localSymbol': contract.localSymbol,
            'strike': contract.strike,
            'date': contract.lastTradeDateOrContractMonth,
            'contract': contract,
            'quantity': int(position)
        }

        # Update existing position or add new
        existing_position = None
        for pos in self.display_positions:
            if pos['localSymbol'] == contract.localSymbol and pos['account'] == account:
                existing_position = pos
                break

        if existing_position:
            existing_position['quantity'] = int(position)
            existing_position['contract'] = contract
            if not startup_complete:
                print(f"Updated position: {account} {contract.symbol} Qty: {position}")
        else:
            self.display_positions.append(display_info)
            if not startup_complete:
                print(f"Added position: {account} {contract.symbol} Qty: {position}")

    def get_open_positions(self) -> List[Dict]:
        """Get list of open positions (quantity > 0)"""
        return [p for p in self.display_positions if p['quantity'] > 0]

    def get_position_by_key(self, account: str, local_symbol: str) -> Optional[Dict]:
        """Get position by account and local symbol"""
        return self.positions.get((account, local_symbol))


class StopOrderManager:
    """Manages stop order placement and tracking"""

    def __init__(self, force_stops_on_loss: bool = False):
        self.stop_order_ids: Dict[str, int] = {}
        self.open_orders: Dict[str, Order] = {}
        self.force_stops_on_loss = force_stops_on_loss

    def track_stop(self, local_symbol: str, order_id: int, order: Order) -> None:
        """Track a stop order"""
        self.stop_order_ids[local_symbol] = order_id
        self.open_orders[local_symbol] = order

    def has_stop(self, local_symbol: str, orders: List[Dict]) -> bool:
        """Check if position already has a stop order"""
        for order_info in orders:
            if (order_info.get('localSymbol') == local_symbol and
                order_info.get('action') == 'SELL' and
                order_info.get('conditions') and
                order_info.get('status') in ['PreSubmitted', 'Submitted']):
                return True
        return False

    def get_stop_order_id(self, local_symbol: str) -> Optional[int]:
        """Get stop order ID for a position"""
        return self.stop_order_ids.get(local_symbol)

    def create_stop_order(self, quantity: int, trigger_price: float,
                         underlying_conid: int, account: str = None) -> Order:
        """Create a conditional stop order"""
        sell_order = Order()
        sell_order.action = "SELL"
        sell_order.orderType = "MKT"
        sell_order.totalQuantity = abs(quantity)
        sell_order.openClose = "C"  # Close position

        if account:
            sell_order.account = account

        # Add price condition
        condition = PriceCondition(
            triggerMethod=2,
            conId=underlying_conid,
            price=trigger_price,
            isMore=False  # Trigger when price drops below
        )
        condition.exchange = 'SMART'
        sell_order.conditions.append(condition)

        return sell_order


class PriceDataManager:
    """Manages historical and current price data"""

    def __init__(self):
        self.historical_prices: Dict[str, List[Dict]] = {}
        self.current_stock_prices: Dict[str, float] = {}
        self.current_option_prices: Dict[str, float] = {}
        self.pending_data: Dict[str, Dict[str, int]] = {}

    def add_historical_bar(self, symbol: str, price: float, date: str) -> None:
        """Add historical price bar"""
        if symbol not in self.historical_prices:
            self.historical_prices[symbol] = []

        self.historical_prices[symbol].append({
            "price": float(price),
            "date": date
        })

    def add_execution_price(self, local_symbol: str, price: float, date: str,
                           shares: float, execution_time: str) -> None:
        """Add execution price data"""
        if local_symbol not in self.historical_prices:
            self.historical_prices[local_symbol] = []

        self.historical_prices[local_symbol].append({
            "price": float(price),
            "date": date,
            "shares": float(shares),
            "execution_time": execution_time
        })

    def calculate_weighted_average(self, local_symbol: str) -> Optional[float]:
        """Calculate weighted average buy price for a position"""
        if local_symbol not in self.historical_prices:
            return None

        executions = self.historical_prices[local_symbol]
        total_shares = sum(e["shares"] for e in executions if "shares" in e)

        if total_shares == 0:
            return None

        weighted_sum = sum(e["price"] * e["shares"] for e in executions if "shares" in e)
        avg_price = weighted_sum / total_shares

        print(f"Calculated weighted avg price for {local_symbol}: ${avg_price:.2f} "
              f"({total_shares} total shares across {len(executions)} executions)")

        return avg_price

    def mark_pending(self, local_symbol: str) -> None:
        """Mark position as waiting for price data"""
        if local_symbol not in self.pending_data:
            self.pending_data[local_symbol] = {'total': 0, 'completed': 0}
        self.pending_data[local_symbol]['total'] += 1

    def mark_completed(self, local_symbol: str) -> bool:
        """Mark price data as received, return True if all data complete"""
        if local_symbol not in self.pending_data:
            self.pending_data[local_symbol] = {'total': 0, 'completed': 0}

        self.pending_data[local_symbol]['completed'] += 1

        return (self.pending_data[local_symbol]['completed'] >=
                self.pending_data[local_symbol]['total'])

    def is_all_data_complete(self) -> bool:
        """Check if all pending data has been received"""
        if not self.pending_data:
            return False

        return all(
            data['completed'] >= data['total']
            for data in self.pending_data.values()
        )


class IBOptionsAutoStop(EWrapper, EClient):
    """Main application class for automated stop-loss placement"""

    def __init__(self, force_stops_on_loss: bool = False):
        EClient.__init__(self, self)

        # Core managers
        self.position_manager = PositionManager()
        self.stop_manager = StopOrderManager(force_stops_on_loss)
        self.price_manager = PriceDataManager()

        # Order and execution tracking
        self.nextOrderId: Optional[int] = None
        self.command_queue = queue.Queue()
        self.orders: List[Dict] = []
        self.execution_details: List[Dict] = []

        # Request ID management
        self.req_id_counter = 1
        self.ticker_id_counter = 1
        self.req_id_to_execution: Dict[int, Dict] = {}

        # Symbol to contract ID mapping
        self.symbol_to_conid: Dict[str, int] = {}

        # State flags
        self.suppress_order_output = False
        self.startup_complete = False

    def nextValidId(self, orderId: int) -> None:
        """Called when connection is established"""
        self.nextOrderId = orderId
        print(f"Next valid order id: {self.nextOrderId}")

        # Subscribe to all order executions
        self.reqAutoOpenOrders(True)
        print("Subscribed to automatic execution notifications")

        # Request current positions
        print("Requesting current positions...")
        self.reqPositions()

        # Request historical executions for stop placement
        print("Requesting historical executions...")
        time.sleep(1)
        self._request_all_executions()

    def execDetails(self, reqId: int, contract: Contract, execution: Execution) -> None:
        """Process execution details"""
        print(f"ExecDetails - ReqId: {reqId}, Symbol: {contract.symbol}, "
              f"Action: {execution.side}, Shares: {execution.shares}, Price: {execution.price}")

        # Only process BUY orders for OPTIONS
        if execution.side != "BOT":
            print(f"  → Skipping {execution.side} execution (not a buy)")
            return

        if contract.secType != "OPT":
            print(f"  → Skipping {contract.secType} (not an option)")
            return

        # Collect for batch processing
        self.execution_details.append({
            'contract': contract,
            'execution': execution
        })

    def execDetailsEnd(self, reqId: int) -> None:
        """Process all collected executions"""
        print(f"All executions received (Total: {len(self.execution_details)})")

        if len(self.execution_details) == 0:
            print("\nNo recent executions found, using avgCost method...")
            self._place_stops_from_avgcost()
            self._print_ready_message()
            return

        # Group by symbol to minimize API calls
        executions_by_symbol = defaultdict(list)
        for ed in self.execution_details:
            symbol = ed['contract'].symbol
            executions_by_symbol[symbol].append(ed)

        print(f"Grouped into {len(executions_by_symbol)} unique symbols")

        # Request contract details for each symbol
        for symbol in executions_by_symbol.keys():
            if symbol not in self.symbol_to_conid:
                self._request_contract_details(symbol)

        # Request historical price data
        for symbol, executions in executions_by_symbol.items():
            self._request_historical_price_batch(symbol, executions)

    def historicalData(self, reqId: int, bar: Any) -> None:
        """Receive historical price bar"""
        req_info = self.req_id_to_execution.get(reqId, {})

        if 'executions' in req_info:
            # Batched request - store all bars
            symbol = req_info['symbol']
            self.price_manager.add_historical_bar(symbol, bar.close, bar.date)
        else:
            # Legacy single execution
            execution = req_info.get('execution')
            local_symbol = req_info.get('contract', {}).get('localSymbol')
            if execution and local_symbol:
                self.price_manager.add_execution_price(
                    local_symbol, bar.close, bar.date,
                    execution.shares, execution.time
                )

    def historicalDataEnd(self, reqId: int, start: str, end: str) -> None:
        """Historical data complete - match prices and place stops"""
        req_info = self.req_id_to_execution.get(reqId, {})

        if 'executions' in req_info:
            self._process_batched_executions(req_info)
        else:
            self._process_single_execution(req_info)

        # Check if all data is complete
        if self.price_manager.is_all_data_complete() and len(self.price_manager.pending_data) > 0:
            print("\nAll execution-based stops processed, checking avgCost positions...")
            self._place_stops_from_avgcost()
            self._print_ready_message()

    def openOrder(self, orderId: int, contract: Contract, order: Order,
                  orderState: OrderState) -> None:
        """Track open orders"""
        order_info = {
            "orderId": orderId,
            "symbol": contract.symbol,
            "localSymbol": contract.localSymbol,
            "secType": contract.secType,
            "action": order.action,
            "orderType": order.orderType,
            "totalQuantity": order.totalQuantity,
            "status": orderState.status,
            "conditions": order.conditions,
            "contract": contract
        }

        # Update or add order
        existing_idx = next((i for i, o in enumerate(self.orders)
                           if o['orderId'] == orderId), None)

        if existing_idx is not None:
            self.orders[existing_idx] = order_info
        else:
            self.orders.append(order_info)

        # Handle BUY order conflicts with existing stops
        if order.action == "BUY" and contract.secType == "OPT":
            self._handle_buy_order_conflict(contract)

        # Display order if not suppressed
        if not self.suppress_order_output:
            self._display_order(orderId, contract, order, orderState)

    def orderStatus(self, orderId: int, status: str, filled: float, remaining: float,
                   avgFillPrice: float, permId: int, parentId: int, lastFillPrice: float,
                   clientId: int, whyHeld: str, mktCapPrice: float) -> None:
        """Handle order status updates and trigger automatic stops"""
        # Display status
        details = []
        if filled > 0:
            details.append(f"Filled: {filled} @ ${avgFillPrice:.2f}")
        if remaining > 0:
            details.append(f"Remaining: {remaining}")
        if whyHeld:
            details.append(f"Held: {whyHeld}")

        details_str = " | ".join(details) if details else ""
        status_msg = f"[IB] Order #{orderId}: {status}"
        if details_str:
            status_msg += f" | {details_str}"
        print(status_msg)

        # Update order status
        for order_info in self.orders:
            if order_info['orderId'] == orderId:
                order_info['status'] = status

                # Auto-place stop when BUY fills
                if (status == "Filled" and
                    order_info.get('action') == 'BUY' and
                    order_info.get('secType') == 'OPT' and
                    filled > 0):
                    self._place_auto_stop_for_filled_order(
                        order_info, avgFillPrice, int(filled)
                    )
                break

    def position(self, account: str, contract: Contract, position: float,
                avgCost: float) -> None:
        """Update position data"""
        if contract.secType == "OPT" and not self.startup_complete:
            print(f"DEBUG avgCost for {contract.localSymbol}: ${avgCost}")

        try:
            self.position_manager.add_or_update(
                account, contract, position, avgCost, self.startup_complete
            )
        except Exception as e:
            print(f"Error updating position: {e}")

    def positionEnd(self) -> None:
        """All positions received"""
        self._display_positions_summary()

        if not self.startup_complete:
            print("\npositionEnd() - requesting contract details...")

            # Request missing contract details
            missing_symbols = set()
            for position in self.position_manager.get_open_positions():
                symbol = position['symbol']
                if symbol not in self.symbol_to_conid:
                    missing_symbols.add(symbol)

            for symbol in missing_symbols:
                self._request_contract_details(symbol)

            self.startup_complete = True

    def contractDetails(self, reqId: int, contractDetails: Any) -> None:
        """Store contract details"""
        conid = contractDetails.contract.conId
        symbol = contractDetails.contract.symbol
        print(f"Received contract details for {symbol} with conId {conid}")
        self.symbol_to_conid[symbol] = conid

    def contractDetailsEnd(self, reqId: int) -> None:
        """Contract details request complete"""
        print(f"Contract details request {reqId} ended")

    def tickPrice(self, reqId: int, tickType: int, price: float, attrib: Any) -> None:
        """Receive current price data"""
        if reqId not in self.req_id_to_execution:
            return

        req_info = self.req_id_to_execution[reqId]
        req_type = req_info.get('type')

        # Stock price (LAST = 4)
        if req_type == 'current_stock_price' and tickType == 4:
            symbol = req_info['symbol']
            self.price_manager.current_stock_prices[symbol] = price
            print(f"Received stock price for {symbol}: ${price:.2f}")
            self.cancelMktData(reqId)

        # Option price (BID = 1 or LAST = 4)
        elif req_type == 'current_option_price' and tickType in [1, 4]:
            local_symbol = req_info['localSymbol']
            if local_symbol not in self.price_manager.current_option_prices:
                self.price_manager.current_option_prices[local_symbol] = price
                print(f"Received option price for {local_symbol}: ${price:.2f}")
                self.cancelMktData(reqId)

    def openOrderEnd(self) -> None:
        """All open orders received"""
        if not self.suppress_order_output:
            print("All open orders retrieved")

    def process_command(self, command: str) -> None:
        """Process user commands"""
        parts = command.strip().split()
        if not parts:
            return

        cmd = parts[0].lower()

        if cmd in ['help', 'h', '?']:
            self._show_help()
        elif cmd in ['positions', 'pos', 'gp']:
            self.reqPositions()
        elif cmd in ['orders', 'ord', 'go']:
            self.reqAllOpenOrders()
        elif cmd in ['executions', 'exec', 'ge']:
            self._request_executions()
        elif cmd in ['refresh', 'reload', 'r']:
            self._refresh_all()
        elif cmd in ['stop', 'ps'] and len(parts) == 3:
            self._handle_manual_stop(parts)
        elif cmd in ['status', 'stat']:
            self._show_status()
        elif cmd in ['quit', 'exit', 'q']:
            self._shutdown()
        else:
            print(f"Unknown command: '{command}'")
            print("Type 'help' for available commands")

    # Private helper methods

    def _request_all_executions(self) -> None:
        """Request historical executions for startup"""
        reqId = self.req_id_counter
        self.req_id_counter += 1

        print(f"Requesting last {Config.EXECUTION_HISTORY_DAYS} days of executions...")

        filt = ExecutionFilter()
        filt.secType = "OPT"
        filt.lastNDays = Config.EXECUTION_HISTORY_DAYS

        self.reqExecutions(reqId, filt)

    def _request_executions(self) -> None:
        """Request today's executions (manual command)"""
        reqId = self.req_id_counter
        self.req_id_counter += 1

        today = datetime.datetime.now().strftime("%Y%m%d 00:00:00 America/New_York")
        print(f"Requesting executions for: {today}")

        filt = ExecutionFilter()
        self.reqExecutions(reqId, filt)

    def _request_contract_details(self, symbol: str) -> None:
        """Request contract details to get conId"""
        contract = Contract()
        contract.symbol = symbol
        contract.secType = "STK"
        contract.currency = "USD"
        contract.exchange = "SMART"
        contract.primaryExchange = "NASDAQ"

        req_id = self.req_id_counter
        self.req_id_counter += 1

        print(f"Requesting contract details for {symbol}...")
        self.reqContractDetails(req_id, contract)
        time.sleep(Config.CONTRACT_DETAIL_DELAY)

    def _request_historical_price_batch(self, symbol: str, executions: List[Dict]) -> None:
        """Request historical prices for all executions of a symbol"""
        # Find earliest execution time
        execution_times = []
        for ed in executions:
            execution = ed['execution']
            exec_time_str, tz_str = execution.time.rsplit(' ', 1)
            exec_time = datetime.datetime.strptime(exec_time_str, "%Y%m%d %H:%M:%S")
            timezone = pytz.timezone(tz_str)
            exec_time = timezone.localize(exec_time)
            execution_times.append((exec_time, ed))

        earliest_time = min(et[0] for et in execution_times)

        # Create contract
        underlying_contract = Contract()
        underlying_contract.symbol = symbol
        underlying_contract.secType = "STK"
        underlying_contract.currency = "USD"
        underlying_contract.exchange = "SMART"
        underlying_contract.primaryExchange = "NASDAQ"

        # Map ticker ID
        ticker_id = self.ticker_id_counter
        self.ticker_id_counter += 1

        self.req_id_to_execution[ticker_id] = {
            'symbol': symbol,
            'executions': executions,
            'underlyingcontract': underlying_contract
        }

        # Mark pending data
        for ed in executions:
            local_symbol = ed['contract'].localSymbol
            self.price_manager.mark_pending(local_symbol)

        # Request data
        utc_time = earliest_time.astimezone(pytz.UTC)
        query_time = utc_time.strftime("%Y%m%d %H:%M:%S UTC")

        print(f"Requesting historical data for {symbol} ({len(executions)} executions)")
        self.reqHistoricalData(ticker_id, underlying_contract, query_time,
                              "1 D", "1 min", "TRADES", 1, 1, False, [])
        time.sleep(Config.HISTORICAL_DATA_DELAY)

    def _process_batched_executions(self, req_info: Dict) -> None:
        """Process batched execution data"""
        symbol = req_info['symbol']
        executions = req_info['executions']

        print(f"Processing {len(executions)} executions for {symbol}")

        bars = self.price_manager.historical_prices.get(symbol, [])

        for ed in executions:
            execution = ed['execution']
            contract = ed['contract']
            local_symbol = contract.localSymbol

            # Match execution to closest bar
            exec_time_str, tz_str = execution.time.rsplit(' ', 1)
            exec_dt = datetime.datetime.strptime(exec_time_str, "%Y%m%d %H:%M:%S")
            timezone = pytz.timezone(tz_str)
            exec_dt = timezone.localize(exec_dt)

            closest_bar = self._find_closest_bar(bars, exec_dt)

            if closest_bar:
                self.price_manager.add_execution_price(
                    local_symbol, closest_bar['price'], closest_bar['date'],
                    execution.shares, execution.time
                )
                print(f"  → Matched {local_symbol} to ${closest_bar['price']:.2f}")

            # Mark complete and check if ready to place stop
            if self.price_manager.mark_completed(local_symbol):
                if not self.stop_manager.has_stop(local_symbol, self.orders):
                    self._place_stop_from_executions(local_symbol)

    def _process_single_execution(self, req_info: Dict) -> None:
        """Process legacy single execution"""
        local_symbol = req_info.get('contract', {}).get('localSymbol')
        if not local_symbol:
            return

        if self.price_manager.mark_completed(local_symbol):
            if not self.stop_manager.has_stop(local_symbol, self.orders):
                self._place_stop_from_executions(local_symbol)

    def _find_closest_bar(self, bars: List[Dict], exec_dt: datetime.datetime) -> Optional[Dict]:
        """Find price bar closest to execution time"""
        closest_bar = None
        min_diff = None

        for bar in bars:
            bar_date_str = bar['date']

            # Parse bar timestamp
            if ' ' in bar_date_str and bar_date_str.count(' ') >= 2:
                date_part, time_part, tz_part = bar_date_str.rsplit(' ', 2)
                bar_dt_str = f"{date_part} {time_part}"
                bar_dt = datetime.datetime.strptime(bar_dt_str, "%Y%m%d %H:%M:%S")
                bar_tz = pytz.timezone(tz_part)
                bar_dt = bar_tz.localize(bar_dt)
            else:
                bar_dt = datetime.datetime.strptime(bar_date_str, "%Y%m%d %H:%M:%S")
                bar_dt = pytz.UTC.localize(bar_dt)

            diff = abs((bar_dt - exec_dt).total_seconds())
            if min_diff is None or diff < min_diff:
                min_diff = diff
                closest_bar = bar

        return closest_bar

    def _place_stop_from_executions(self, local_symbol: str) -> None:
        """Place stop order using execution data"""
        avg_price = self.price_manager.calculate_weighted_average(local_symbol)
        if avg_price is None:
            print(f"Error: Could not calculate average price for {local_symbol}")
            return

        # Find execution data
        exec_contract = None
        symbol = None
        execution = None

        for ed in self.execution_details:
            if ed['contract'].localSymbol == local_symbol:
                exec_contract = ed['contract']
                symbol = exec_contract.symbol
                execution = ed['execution']
                break

        if not exec_contract or not execution:
            print(f"Error: No execution found for {local_symbol}")
            return

        # Get position
        position_key = (execution.acctNumber, local_symbol)
        position = self.position_manager.get_position_by_key(
            execution.acctNumber, local_symbol
        )

        if not position or position['position'] == 0:
            return

        # Get underlying conId
        underlying_conid = self.symbol_to_conid.get(symbol)
        if not underlying_conid:
            print(f"Error: Missing conId for {symbol}")
            return

        # Calculate trigger and create order
        trigger_price = avg_price * (1 - Config.STOP_LOSS_PERCENTAGE)

        sell_order = self.stop_manager.create_stop_order(
            position['position'], trigger_price, underlying_conid, position['account']
        )

        # Place order
        order_id = self.nextOrderId

        contract = Contract()
        contract.symbol = exec_contract.symbol
        contract.localSymbol = exec_contract.localSymbol
        contract.secType = "OPT"
        contract.exchange = "SMART"
        contract.currency = "USD"

        self.placeOrder(order_id, contract, sell_order)

        print(f"\n{'='*Config.SEPARATOR_SHORT}")
        print(f"STOP ORDER PLACED")
        print(f"{'='*Config.SEPARATOR_SHORT}")
        print(f"  Position: {local_symbol}")
        print(f"  Quantity: {sell_order.totalQuantity}")
        print(f"  Avg Buy: ${avg_price:.2f}")
        print(f"  Trigger: ${trigger_price:.2f}")
        print(f"  Order ID: {order_id}")
        print(f"{'='*Config.SEPARATOR_SHORT}\n")

        self.stop_manager.track_stop(local_symbol, order_id, sell_order)
        self.nextOrderId += 1

    def _place_auto_stop_for_filled_order(self, order_info: Dict,
                                         avg_fill_price: float, filled: int) -> None:
        """Place stop immediately when BUY order fills"""
        local_symbol = order_info.get('localSymbol')
        symbol = order_info.get('symbol')
        contract = order_info.get('contract')

        if not (contract and symbol):
            print(f"  Warning: Missing contract info")
            return

        underlying_conid = self.symbol_to_conid.get(symbol)
        if not underlying_conid:
            print(f"  Warning: No conId found for {symbol}")
            return

        print(f"\n[AUTO] BUY filled for {local_symbol} - placing stop...")

        trigger_price = avg_fill_price * (1 - Config.STOP_LOSS_PERCENTAGE)
        sell_order = self.stop_manager.create_stop_order(
            filled, trigger_price, underlying_conid
        )

        order_id = self.nextOrderId
        self.placeOrder(order_id, contract, sell_order)

        print(f"  Stop placed: Order #{order_id} at ${trigger_price:.2f}")

        self.stop_manager.track_stop(local_symbol, order_id, sell_order)
        self.nextOrderId += 1

    def _place_stops_from_avgcost(self) -> None:
        """Place stops using avgCost method for older positions"""
        print(f"\n{'='*Config.SEPARATOR_SHORT}")
        print("Checking positions needing stops (avgCost method)...")
        print(f"{'='*Config.SEPARATOR_SHORT}")

        time.sleep(2)  # Allow time for data to arrive

        positions_needing_data = []

        for position_key, position_info in self.position_manager.positions.items():
            account, local_symbol = position_key

            # Skip non-options and closed positions
            if position_info['secType'] != 'OPT':
                continue
            if abs(position_info['position']) == 0:
                continue

            # Skip if has execution data or existing stop
            if local_symbol in self.price_manager.historical_prices:
                continue
            if self.stop_manager.has_stop(local_symbol, self.orders):
                continue

            # Check if we need price data
            symbol = position_info['symbol']
            need_stock = symbol not in self.price_manager.current_stock_prices
            need_option = local_symbol not in self.price_manager.current_option_prices

            if need_stock or need_option:
                if need_stock:
                    self._request_current_stock_price(symbol, local_symbol)
                if need_option:
                    self._request_current_option_price(local_symbol, position_info)
                positions_needing_data.append((position_key, position_info))
                continue

            # Place stop with available data
            self._place_stop_with_avgcost(position_key, position_info)

        if positions_needing_data:
            print(f"\n  Waiting for price data on {len(positions_needing_data)} positions...")
            threading.Thread(
                target=self._retry_avgcost_placement,
                daemon=True
            ).start()

        print(f"{'='*Config.SEPARATOR_SHORT}\n")

    def _place_stop_with_avgcost(self, position_key: Tuple, position_info: Dict) -> None:
        """Place stop using avgCost and current prices"""
        account, local_symbol = position_key
        symbol = position_info['symbol']

        avg_cost_per_contract = float(position_info['avgCost'])
        avg_cost_per_share = avg_cost_per_contract / 100.0

        current_stock_price = self.price_manager.current_stock_prices[symbol]
        current_option_price = self.price_manager.current_option_prices[local_symbol]

        underlying_conid = self.symbol_to_conid.get(symbol)
        if not underlying_conid:
            print(f"  Missing conId for {symbol}")
            return

        # Calculate profit/loss
        profit_per_share = current_option_price - avg_cost_per_share
        profit_percent = (profit_per_share / avg_cost_per_share * 100
                         if avg_cost_per_share > 0 else 0)

        print(f"\n  Position: {local_symbol}")
        print(f"    Bought: ${avg_cost_per_share:.2f}/share")
        print(f"    Current: ${current_option_price:.2f}/share")
        print(f"    P/L: ${profit_per_share:.2f} ({profit_percent:+.1f}%)")

        # Check if at loss
        if profit_per_share <= 0 and not self.stop_manager.force_stops_on_loss:
            print(f"    WARNING: At loss - manual stop recommended")
            return

        # Place stop
        trigger_price = current_stock_price * (1 - Config.STOP_LOSS_PERCENTAGE)

        contract = Contract()
        contract.symbol = symbol
        contract.localSymbol = local_symbol
        contract.secType = "OPT"
        contract.exchange = "SMART"
        contract.currency = "USD"

        sell_order = self.stop_manager.create_stop_order(
            abs(position_info['position']), trigger_price, underlying_conid, account
        )

        order_id = self.nextOrderId
        self.placeOrder(order_id, contract, sell_order)

        print(f"    Stop placed: Order #{order_id} at ${trigger_price:.2f}")

        self.stop_manager.track_stop(local_symbol, order_id, sell_order)
        self.nextOrderId += 1

    def _retry_avgcost_placement(self) -> None:
        """Retry placing stops after delay"""
        time.sleep(Config.AVGCOST_RETRY_DELAY)
        print("\n[AUTO-RETRY] Retrying stop placement...")
        self._place_stops_from_avgcost()

    def _request_current_stock_price(self, symbol: str, local_symbol: str) -> None:
        """Request current stock price"""
        contract = Contract()
        contract.symbol = symbol
        contract.secType = "STK"
        contract.currency = "USD"
        contract.exchange = "SMART"

        ticker_id = self.ticker_id_counter
        self.ticker_id_counter += 1

        self.req_id_to_execution[ticker_id] = {
            'symbol': symbol,
            'localSymbol': local_symbol,
            'type': 'current_stock_price'
        }

        self.reqMktData(ticker_id, contract, "", True, False, [])

    def _request_current_option_price(self, local_symbol: str, position_info: Dict) -> None:
        """Request current option price"""
        contract = Contract()
        contract.symbol = position_info['symbol']
        contract.localSymbol = local_symbol
        contract.secType = "OPT"
        contract.currency = "USD"
        contract.exchange = "SMART"

        ticker_id = self.ticker_id_counter
        self.ticker_id_counter += 1

        self.req_id_to_execution[ticker_id] = {
            'symbol': position_info['symbol'],
            'localSymbol': local_symbol,
            'type': 'current_option_price'
        }

        self.reqMktData(ticker_id, contract, "", True, False, [])

    def _handle_buy_order_conflict(self, contract: Contract) -> None:
        """Cancel existing stops when new BUY order is placed"""
        local_symbol = contract.localSymbol
        existing_stop_id = self.stop_manager.get_stop_order_id(local_symbol)

        if existing_stop_id:
            print(f"\n[!] Detected BUY for {local_symbol}")
            print(f"    Cancelling existing stop (Order #{existing_stop_id})")
            self.cancelOrder(existing_stop_id, OrderCancel())
        else:
            # Check for stops from previous session
            for order in self.orders:
                if (order.get('localSymbol') == local_symbol and
                    order.get('action') == 'SELL' and
                    order.get('status') in ['PreSubmitted', 'Submitted']):
                    cancel_id = order['orderId']
                    print(f"\n[!] Found existing SELL (Order #{cancel_id})")
                    print(f"    Cancelling to allow buy...")
                    self.cancelOrder(cancel_id, OrderCancel())
                    break

    def _display_order(self, order_id: int, contract: Contract,
                      order: Order, order_state: OrderState) -> None:
        """Display order in compact format"""
        symbol = contract.localSymbol or contract.symbol
        action_str = f"{order.action} {order.totalQuantity}"

        condition_str = ""
        if order.conditions:
            for condition in order.conditions:
                if hasattr(condition, 'price'):
                    operator = "<=" if not condition.isMore else ">="
                    condition_str = f" | Trigger {operator} ${condition.price:.2f}"
                    break

        print(f"Order #{order_id} [{order_state.status}]: {symbol} {action_str}{condition_str}")

    def _display_positions_summary(self) -> None:
        """Display positions summary"""
        open_positions = self.position_manager.get_open_positions()

        print(f"\n{'='*Config.SEPARATOR_WIDTH}")
        print(f"POSITIONS SUMMARY ({len(open_positions)} positions)")
        print(f"{'='*Config.SEPARATOR_WIDTH}\n")

        if not open_positions:
            print("  No open positions\n")
            return

        for idx, position in enumerate(open_positions):
            local_symbol = position['localSymbol']

            # Find stop info
            stop_info = "NONE"
            for order in self.orders:
                if (order.get('localSymbol') == local_symbol and
                    order.get('action') == 'SELL' and
                    order.get('conditions')):
                    for condition in order['conditions']:
                        if hasattr(condition, 'price'):
                            stop_info = f"${condition.price:.2f}"
                            break
                    break

            # Display position
            print(f"[{idx}] {position['symbol']:6s} ${position['strike']}{position['right']} "
                  f"{position['date']} | Qty: {position['quantity']:2d} | "
                  f"Stop: {stop_info:8s} | Acct: {position['account']}")

        print(f"{'='*Config.SEPARATOR_WIDTH}\n")

    def _handle_manual_stop(self, parts: List[str]) -> None:
        """Handle manual stop placement command"""
        try:
            index = int(parts[1])
            trigger_price = float(parts[2])

            open_positions = self.position_manager.get_open_positions()
            if index < 0 or index >= len(open_positions):
                print(f"Invalid index: {index}")
                return

            position_info = open_positions[index]
            local_symbol = position_info['localSymbol']
            symbol = position_info['symbol']

            # Check for existing stop
            existing_order_id = None
            existing_order = None
            for order in self.orders:
                if (order.get('localSymbol') == local_symbol and
                    order.get('action') == 'SELL' and
                    order.get('conditions') and
                    order.get('status') in ['PreSubmitted', 'Submitted']):
                    existing_order_id = order['orderId']
                    existing_order = order
                    break

            # Get underlying conId
            underlying_conid = self.symbol_to_conid.get(symbol)
            if not underlying_conid:
                print(f"Error: Missing conId for {symbol}")
                return

            # Create order
            sell_order = self.stop_manager.create_stop_order(
                position_info['quantity'], trigger_price, underlying_conid,
                position_info['account']
            )

            # Use existing contract if modifying
            if existing_order_id and 'contract' in existing_order:
                contract = existing_order['contract']
                print(f"Modifying stop order #{existing_order_id} for {local_symbol}")
                print(f"  Old: ${existing_order['conditions'][0].price:.2f} → "
                      f"New: ${trigger_price:.2f}")
                self.placeOrder(existing_order_id, contract, sell_order)
            else:
                contract = position_info['contract']
                contract.exchange = 'SMART'
                print(f"Placing new stop for {local_symbol} at ${trigger_price:.2f}")
                self.placeOrder(self.nextOrderId, contract, sell_order)
                self.nextOrderId += 1

        except ValueError:
            print("Error: Invalid format. Usage: stop <index> <price>")

    def _refresh_all(self) -> None:
        """Refresh positions and executions"""
        print("Refreshing positions and executions...")
        self.reqPositions()
        time.sleep(1)
        self._request_executions()

    def _show_help(self) -> None:
        """Display help message"""
        print(f"\n{'='*Config.SEPARATOR_SHORT}")
        print("Available Commands:")
        print(f"{'='*Config.SEPARATOR_SHORT}")
        print("  positions, gp           - Show all current positions")
        print("  orders, go              - Show all open orders")
        print("  executions, ge          - Get executions and place stops")
        print("  stop <idx> <price>, ps  - Place/modify stop order")
        print("  refresh, r              - Refresh positions and executions")
        print("  status                  - Show system status")
        print("  help, h, ?              - Show this help")
        print("  quit, exit, q           - Exit program")
        print(f"{'='*Config.SEPARATOR_SHORT}\n")

    def _show_status(self) -> None:
        """Display system status"""
        print(f"\n{'='*Config.SEPARATOR_SHORT}")
        print("System Status:")
        print(f"{'='*Config.SEPARATOR_SHORT}")
        print(f"  Connected: {self.isConnected()}")
        print(f"  Next Order ID: {self.nextOrderId}")
        print(f"  Open Positions: {len(self.position_manager.get_open_positions())}")
        print(f"  Tracked Orders: {len(self.stop_manager.open_orders)}")
        print(f"  Execution Details: {len(self.execution_details)}")
        print(f"  Known ConIds: {len(self.symbol_to_conid)}")
        print(f"{'='*Config.SEPARATOR_SHORT}\n")

    def _shutdown(self) -> None:
        """Disconnect and exit"""
        print("Disconnecting...")
        self.disconnect()
        import sys
        sys.exit(0)

    def _print_ready_message(self) -> None:
        """Print ready message"""
        print(f"\n{'='*Config.SEPARATOR_SHORT}")
        print("READY - All startup tasks complete")
        print("Type 'help' for available commands")
        print(f"{'='*Config.SEPARATOR_SHORT}\n")


def run_loop(app: IBOptionsAutoStop) -> None:
    """Run the IB client loop"""
    app.run()


def command_input_loop(command_queue: queue.Queue) -> None:
    """Read user input and queue commands"""
    while True:
        command = input("Enter command: ")
        command_queue.put(command)


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description='Automated stop-loss placement for Interactive Brokers options',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  python autosell.py                       # Normal mode
  python autosell.py --force-stops-on-loss # Place stops on all positions

Notes:
  - Fresh executions get 1% stop from actual buy price
  - Older positions use avgCost method with profit validation
'''
    )
    parser.add_argument(
        '--force-stops-on-loss',
        action='store_true',
        help='Place stops on all positions, even if at a loss'
    )
    args = parser.parse_args()

    # Create app instance
    app = IBOptionsAutoStop(force_stops_on_loss=args.force_stops_on_loss)

    # Connect to TWS
    app.connect(Config.TWS_HOST, Config.TWS_PORT, Config.CLIENT_ID)

    if args.force_stops_on_loss:
        print(f"\n{'='*Config.SEPARATOR_SHORT}")
        print("WARNING: --force-stops-on-loss enabled")
        print("Stops will be placed on ALL positions!")
        print(f"{'='*Config.SEPARATOR_SHORT}\n")

    # Start threads
    api_thread = threading.Thread(target=run_loop, args=(app,), daemon=True)
    api_thread.start()

    command_thread = threading.Thread(
        target=command_input_loop,
        args=(app.command_queue,),
        daemon=True
    )
    command_thread.start()

    # Wait for connection
    time.sleep(1)
    while app.nextOrderId is None:
        time.sleep(0.1)

    # Main loop
    try:
        while True:
            if not app.command_queue.empty():
                command = app.command_queue.get()
                app.process_command(command)
            time.sleep(1)
    except KeyboardInterrupt:
        print("Disconnecting from TWS...")
        app.disconnect()


if __name__ == "__main__":
    main()
