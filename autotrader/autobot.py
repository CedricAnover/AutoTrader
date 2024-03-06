import os
import importlib
import traceback
import pandas as pd
from datetime import datetime, timezone
from autotrader.strategy import Strategy
from autotrader.utilities import DataStream
from autotrader.brokers.trading import Order
from autotrader.brokers.broker import Broker
from typing import TYPE_CHECKING, Literal, Union
from autotrader.brokers.virtual.broker import Broker as VirtualBroker
from autotrader.utilities import get_data_config, TradeAnalysis, get_logger

if TYPE_CHECKING:
    from autotrader import AutoTrader
    from autotrader.comms.notifier import Notifier


class AutoTraderBot:
    """AutoTrader Trading Bot, responsible for a trading strategy."""

    def __init__(
        self,
        instrument: str,
        strategy_dict: dict,
        broker: Union[dict[str, Broker], Broker],
        deploy_dt: datetime,
        data_dict: dict,
        quote_data_path: str,
        auxdata: dict,
        autotrader_instance: "AutoTrader",
    ) -> None:
        """Instantiates an AutoTrader Bot.

        Parameters
        ----------
        instrument : str
            The trading instrument assigned to the bot instance.

        strategy_dict : dict
            The strategy configuration dictionary.

        broker : AutoTrader Broker instance
            The AutoTrader Broker module.

        deploy_dt : datetime
            The datetime stamp of the bot deployment time.

        data_dict : dict
            The strategy data.

        quote_data_path : str
            The quote data filepath for the trading instrument
            (for backtesting only).

        auxdata : dict
            Auxiliary strategy data.

        autotrader_instance : AutoTrader
            The parent AutoTrader instance.

        Raises
        ------
        Exception
            When there is an error retrieving the instrument data.
        """
        # Type hint inherited attributes
        self._run_mode: Literal["continuous", "periodic"]
        self._data_indexing: Literal["open", "close"]
        self._multiple_brokers: bool
        self._global_config_dict: dict[str, any]
        self._environment: Literal["paper", "live"]
        self._broker_name: str
        self._feed: str
        self._allow_dancing_bears: bool
        self._base_currency: str
        self._data_path_mapper: dict
        self._backtest_mode: bool
        self._data_directory: str
        self._papertrading: bool
        self._max_workers: int
        self._scan_mode: bool
        self._notify: int
        self._verbosity: int
        self._notifier: "Notifier"
        self._virtual_tradeable_instruments: dict[str, list[str]]
        self._logger_kwargs: dict
        self._data_stream_object: DataStream
        self._data_start: datetime
        self._data_end: datetime
        self._dynamic_data: bool
        self._req_liveprice: bool

        # Inherit user options from autotrader
        for attribute, value in autotrader_instance.__dict__.items():
            setattr(self, attribute, value)

        # Create autobot logger
        self.logger = get_logger(name="autobot", **self._logger_kwargs)

        # Assign local attributes
        self.instrument = instrument
        self._broker: Broker = broker

        # Define execution framework
        if self._execution_method is None:
            self._execution_method = self._submit_order

        # Check for muliple brokers and construct mapper
        if self._multiple_brokers:
            # Trading across multiple venues
            self._brokers: list[Broker] = self._broker
            self._instrument_to_broker = {}
            for (
                broker_name,
                tradeable_instruments,
            ) in self._virtual_tradeable_instruments.items():
                for instrument in tradeable_instruments:
                    if instrument in self._instrument_to_broker:
                        # Instrument is already in mapper, add broker
                        self._instrument_to_broker[instrument].append(
                            self._brokers[broker_name]
                        )
                    else:
                        # New instrument, add broker
                        self._instrument_to_broker[instrument] = [
                            self._brokers[broker_name]
                        ]

        else:
            # Trading through a single broker
            self._brokers = {self._broker_name: self._broker}

            # Map instruments to broker
            self._instrument_to_broker = {}
            instruments = [instrument] if isinstance(instrument, str) else instrument
            for instrument in instruments:
                self._instrument_to_broker[instrument] = [self._broker]

        # Unpack strategy parameters and assign to strategy_params
        strategy_config = strategy_dict["config"]
        interval = strategy_config["INTERVAL"]
        period = strategy_config["PERIOD"]
        risk_pc = strategy_config["RISK_PC"] if "RISK_PC" in strategy_config else None
        sizing = strategy_config["SIZING"] if "SIZING" in strategy_config else None
        params = (
            strategy_config["PARAMETERS"] if "PARAMETERS" in strategy_config else {}
        )
        strategy_params = params
        strategy_params["granularity"] = (
            strategy_params["granularity"]
            if "granularity" in strategy_params
            else interval
        )
        strategy_params["risk_pc"] = (
            strategy_params["risk_pc"] if "risk_pc" in strategy_params else risk_pc
        )
        strategy_params["sizing"] = (
            strategy_params["sizing"] if "sizing" in strategy_params else sizing
        )
        strategy_params["period"] = (
            strategy_params["period"] if "period" in strategy_params else period
        )
        strategy_params["INCLUDE_POSITIONS"] = (
            strategy_config["INCLUDE_POSITIONS"]
            if "INCLUDE_POSITIONS" in strategy_config
            else False
        )
        strategy_config["INCLUDE_BROKER"] = (
            strategy_config["INCLUDE_BROKER"]
            if "INCLUDE_BROKER" in strategy_config
            else False
        )
        strategy_config["INCLUDE_STREAM"] = (
            strategy_config["INCLUDE_STREAM"]
            if "INCLUDE_STREAM" in strategy_config
            else False
        )
        self._strategy_params = strategy_params

        # Import Strategy
        if strategy_dict["class"] is not None:
            strategy = strategy_dict["class"]
        else:
            strat_module = strategy_config["MODULE"]
            strat_name = strategy_config["CLASS"]
            strat_package_path = os.path.join(self._home_dir, "strategies")
            strat_module_path = os.path.join(strat_package_path, strat_module) + ".py"
            strat_spec = importlib.util.spec_from_file_location(
                strat_module, strat_module_path
            )
            strategy_module = importlib.util.module_from_spec(strat_spec)
            strat_spec.loader.exec_module(strategy_module)
            strategy = getattr(strategy_module, strat_name)

        # Strategy shutdown routine
        self._strategy_shutdown_method = strategy_dict["shutdown_method"]

        # Data retrieval
        self._quote_data_file = quote_data_path  # Either str or None
        self._data_filepaths = data_dict  # Either str or dict, or None
        self._auxdata_files = auxdata  # Either str or dict, or None

        if self._feed == "none":
            # None data-feed being used, allow duplicate bars
            self._allow_duplicate_bars = True

        # Check for portfolio strategy
        trade_portfolio = (
            strategy_config["PORTFOLIO"] if "PORTFOLIO" in strategy_config else False
        )

        portfolio = strategy_config["WATCHLIST"] if trade_portfolio else False

        # ~~~~~~~~~~~~~~~~~~~~~~~~ BELOW TO BE DELETED ~~~~~~~~~~~~~~~~~~~~~~~~
        # Get data feed configuration
        # self._data_config = get_data_config(
        #     feed=self._feed,
        #     global_config=self._global_config_dict,
        #     environment=self._environment,
        # )

        # Create instance of data stream object
        # Only when using local data...
        # TODO - I think the way this will have to work is: brokers get instantiated
        # from autotrader, the datastream gets constructed and passed to the brokers
        # (or something similar), and then here, only the broker data methods get called
        # to get the latest strategy data.
        # But then should every exchange have their own datastream object? Why dont the data
        # methods just get put in there?
        # Each exchange has its own data stream... dont need to do anything here to
        # instantiate the datastream.
        # self._stream_attributes = {
        #     "instrument": self.instrument,
        #     "feed": self._feed,
        #     "data_filepaths": self._data_filepaths,
        #     "quote_data_file": self._quote_data_file,
        #     "auxdata_files": self._auxdata_files,
        #     "strategy_params": self._strategy_params,
        #     # "get_data": self._get_data,
        #     "data_start": self._data_start,
        #     "data_end": self._data_end,
        #     "portfolio": portfolio,
        #     "data_path_mapper": self._data_path_mapper,
        #     "data_dir": self._data_directory,
        #     "backtest_mode": self._backtest_mode,
        # }
        # self.datastream: DataStream = self._data_stream_object(**stream_attributes)
        # ~~~~~~~~~~~~~~~~~~~~~~~~ ABOVE TO BE DELETED ~~~~~~~~~~~~~~~~~~~~~~~~

        # TODO - need to initialise the broker with a cache of data, if backtesting over
        # full dataset, or otherwise just the window specified in the strategy.
        # Initialise broker datasets
        # The block below needs to be tidied with data config handling.

        for instrument, brokers in self._instrument_to_broker.items():
            for broker in brokers:
                # TODO - what if non virtual brokers are used?
                broker._initialise_data(
                    **{
                        "instrument": instrument,
                        "data_start": self._data_start,
                        "data_end": self._data_end,
                        "granularity": interval,
                    }
                )

        # Build strategy instantiation arguments
        strategy_inputs = {
            "parameters": params,
            # "data": self._strat_data,
            "instrument": self.instrument,
            "broker": self._broker,
        }

        # Instantiate Strategy
        my_strat: Strategy = strategy(**strategy_inputs)

        # Assign strategy to local attributes
        self._last_bars = None
        self._strategy = my_strat
        self._strategy_name = (
            strategy_config["NAME"]
            if "NAME" in strategy_config
            else "(unnamed strategy)"
        )

        # Assign stop trading method to strategy
        self._strategy.stop_trading = autotrader_instance._remove_instance_file

        # Assign strategy attributes for tick-based strategy development
        # TODO - improve type hints using strategy base class
        if self._backtest_mode:
            self._strategy._backtesting = True
            self.trade_results = None
        if interval.split(",")[0] == "tick":
            self._strategy._tick_data = True

    def __repr__(self):
        if isinstance(self.instrument, list):
            return "Portfolio AutoTraderBot"
        else:
            return f"{self.instrument} AutoTraderBot"

    def __str__(self):
        return "AutoTraderBot instance"

    def _update(self, timestamp: datetime) -> None:
        """Update strategy with the latest data and generate a trade signal.

        Parameters
        ----------
        timestamp : datetime, optional
            The current update time.
        """
        if self._backtest_mode or self._papertrading:
            # Update virtual broker
            self._update_virtual_broker(dt=timestamp)

        # Call strategy for orders
        strategy_orders = self._strategy.generate_signal(timestamp)

        # Check and qualify orders
        orders = self._check_orders(strategy_orders)
        self._qualify_orders(orders)

        if not self._scan_mode:
            # Submit orders
            for order in orders:
                # Submit order to relevant exchange
                try:
                    self._execution_method(
                        broker=self._brokers[order.exchange],
                        order=order,
                        order_time=timestamp,
                    )
                except Exception as e:
                    traceback_str = "".join(traceback.format_tb(e.__traceback__))
                    exception_str = f"AutoTrader exception when submitting order: {e}"
                    print_str = exception_str + "\nTraceback:\n" + traceback_str
                    self.logger.error(print_str)

        # If paper trading, update virtual broker again to trigger any orders
        if self._papertrading:
            self._update_virtual_broker(dt=timestamp)

        # Log message
        current_time = timestamp.strftime("%b %d %Y %H:%M:%S")
        if len(orders) > 0:
            for order in orders:
                direction = "long" if order.direction > 0 else "short"
                order_string = (
                    f"{current_time}: {order.instrument} "
                    + f"{direction} {order.order_type} order of "
                    + f"{order.size} units placed."
                )
                self.logger.info(order_string)
        else:
            self.logger.debug(
                f"{current_time}: No signal detected ({self.instrument})."
            )

        # Check for orders placed and/or scan hits
        if int(self._notify) > 0 and not (self._backtest_mode or self._scan_mode):
            # TODO - what is this conditional?
            for order in orders:
                self._notifier.send_order(order)

        # Check scan results
        if self._scan_mode:
            # Report AutoScan results
            if int(self._verbosity) > 0 or int(self._notify) == 0:
                # Scan reporting with no notifications requested
                if len(orders) == 0:
                    print(f"{self.instrument}: No signal detected.")

                else:
                    # Scan detected hits
                    print("Scan hits:")
                    for order in orders:
                        print(order)

            if int(self._notify) > 0:
                # Notifications requested
                for order in orders:
                    self._notifier.send_message(f"Scan hit: {order}")

    def _refresh_data(self, timestamp: datetime = None, **kwargs):
        """Refreshes the active Bot's data attributes for trading.

        When backtesting without dynamic data updates, the data attributes
        of the bot will be constant. When using dynamic data, or when
        livetrading in continuous mode, the data attributes will change
        as time passes, reflecting more up-to-date data. This method refreshes
        the data attributes for a given timestamp by calling the datastream
        object.

        Parameters
        ----------
        timestamp : datetime, optional
            The current timestamp. If None, datetime.now() will be called.
            The default is None.

        **kwargs : dict
            Any other named arguments.

        Raises
        ------
        Exception
            When there is an error retrieving the data.

        Returns
        -------
        None:
            The up-to-date data will be assigned to the Bot instance.
        """
        # TODO - this method is now obsolete - delete it

        timestamp = datetime.now(timezone.utc) if timestamp is None else timestamp

        # Fetch new data
        # TODO - rename - timestamp is a datetimte object

        # How many brokers are there? There could be multiple
        # What if I just didn't get candles here? Instead just pass timestamp to strategy,
        # and allow them to call candles if it wants...
        # or any other broker methods.
        # self._broker.data_broker.get_candles

        data, multi_data, quote_data, auxdata = self.datastream.refresh(
            timestamp=timestamp
        )

        # Check data returned is valid
        if self._feed != "none" and len(data) == 0:
            raise Exception("Error retrieving data.")

        # Data assignment
        if multi_data is None:
            strat_data = data
        else:
            strat_data = multi_data

        # Auxiliary data assignment
        if auxdata is not None:
            strat_data = {"base": strat_data, "aux": auxdata}

        # Assign data attributes to bot
        self._strat_data = strat_data
        self.data = data
        self.multi_data = multi_data
        self.auxdata = auxdata
        self.quote_data = quote_data

    def _check_orders(self, orders) -> list[Order]:
        """Checks that orders returned from strategy are in the correct
        format.

        Returns
        -------
        List of Orders

        Notes
        -----
        An order must have (at the very least) an order type specified. Usually,
        the direction will also be required, except in the case of close order
        types. If an order with no order type is provided, it will be ignored.
        """

        def check_type(orders):
            checked_orders = []
            if isinstance(orders, dict):
                # Order(s) provided in dictionary
                if "order_type" in orders:
                    # Single order dict provided
                    if "instrument" not in orders:
                        orders["instrument"] = self.instrument
                    checked_orders.append(Order._from_dict(orders))

                elif len(orders) > 0:
                    # Multiple orders provided
                    for key, item in orders.items():
                        if isinstance(item, dict) and "order_type" in item:
                            # Convert order dict to Order object
                            if "instrument" not in item:
                                item["instrument"] = self.instrument
                            checked_orders.append(Order._from_dict(item))
                        elif isinstance(item, Order):
                            # Native Order object, append as is
                            checked_orders.append(item)
                        else:
                            raise Exception(f"Invalid order submitted: {item}")

                elif len(orders) == 0:
                    # Empty order dict
                    pass

            elif isinstance(orders, Order):
                # Order object directly returned
                checked_orders.append(orders)

            elif isinstance(orders, list):
                # Order(s) provided in list
                for item in orders:
                    if isinstance(item, dict) and "order_type" in item:
                        # Convert order dict to Order object
                        if "instrument" not in item:
                            item["instrument"] = self.instrument
                        checked_orders.append(Order._from_dict(item))
                    elif isinstance(item, Order):
                        # Native Order object, append as is
                        checked_orders.append(item)
                    else:
                        raise Exception(f"Invalid order submitted: {item}")
            else:
                raise Exception(f"Invalid order/s submitted: '{orders}' received")

            return checked_orders

        def add_strategy_data(orders):
            # Append strategy parameters to each order
            for order in orders:
                order.instrument = (
                    self.instrument if not order.instrument else order.instrument
                )
                order.strategy = (
                    self._strategy.name
                    if "name" in self._strategy.__dict__
                    else self._strategy_name
                )
                order.granularity = self._strategy_params["granularity"]
                order._sizing = self._strategy_params["sizing"]
                order._risk_pc = self._strategy_params["risk_pc"]

        def check_order_details(orders: list[Order]) -> None:
            # Check details for order type have been provided
            for ix, order in enumerate(orders):
                order.instrument = (
                    order.instrument
                    if order.instrument is not None
                    else self.instrument
                )
                if order.order_type in ["market", "limit", "stop-limit", "reduce"]:
                    if not order.direction:
                        # Order direction was not provided, delete order
                        del orders[ix]
                        continue

                # Check that an exchange has been specified
                if order.exchange is None:
                    # Exchange not specified
                    if self._multiple_brokers:
                        # Trading across multiple venues
                        raise Exception(
                            "The exchange to which an order is to be "
                            + "submitted must be specified when trading across "
                            + "multiple venues. Please include the 'exchange' "
                            + "argument when creating an order."
                        )
                    else:
                        # Trading on single venue, auto fill
                        order.exchange = self._broker_name

        # Perform checks
        if orders is not None:
            checked_orders = check_type(orders)
            add_strategy_data(checked_orders)
            check_order_details(checked_orders)
            return checked_orders
        else:
            # Return empty list
            return []

    def _qualify_orders(self, orders: list[Order]) -> None:
        """Prepare orders for submission."""
        for order in orders:
            # Get relevant broker
            broker: Broker = self._brokers[order.exchange]

            # Fetch precision for instrument
            try:
                precision = broker.get_precision(order.instrument)
            except Exception as e:
                # Print exception
                self.logger.error("AutoTrader exception when qualifying order:", e)

                # Skip this order
                continue

            # Determine current price to assign to order
            if self._feed != "none":
                # Get order price from current orderbook
                orderbook = broker.get_orderbook(
                    instrument=order.instrument,
                )

                # Check order type to assign variables
                # TODO - review use of HCF
                if order.direction < 0:
                    order_price = orderbook.bids.loc[0]["price"]
                    HCF = 1
                else:
                    order_price = orderbook.asks.loc[0]["price"]
                    HCF = 1

            else:
                # Do not provide order price yet
                order_price = None
                HCF = None

            # Call order to update
            order(broker=broker, order_price=order_price, HCF=HCF, precision=precision)

    def _update_virtual_broker(self, dt: datetime) -> None:
        """Updates the virtual broker state. Only called when backtesting or paper trading."""
        for instrument, brokers in self._instrument_to_broker.items():
            for broker in brokers:
                broker: VirtualBroker
                broker._update_positions(instrument=instrument, dt=dt)

    def _create_trade_results(self, broker_histories: dict) -> dict:
        """Constructs bot-specific trade summary for post-processing."""
        trade_results = TradeAnalysis(self._broker, broker_histories, self.instrument)
        trade_results.indicators = (
            self._strategy.indicators if hasattr(self._strategy, "indicators") else None
        )
        trade_results.data = self._broker._data_cache[self.instrument]
        trade_results.interval = self._strategy_params["granularity"]
        self.trade_results = trade_results

    def _get_iteration_range(self) -> int:
        """Checks mode of operation and returns data iteration range. For backtesting,
        the entire dataset is iterated over. For livetrading, only the latest candle
        is used. ONLY USED IN BACKTESTING NOW.
        """
        # TODO - this method is now obsolete - delete it
        start_range = self._strategy_params["period"]
        end_range = len(self.data)

        if len(self.data) < start_range:
            raise Exception(
                "There are not enough bars in the data to "
                + "run the backtest with the current strategy "
                + "configuration settings. Either extend the "
                + "backtest period, or reduce the PERIOD key of "
                + "your strategy configuration."
            )

        return start_range, end_range

    @staticmethod
    def _check_ohlc_data(
        ohlc_data: pd.DataFrame,
        timestamp: datetime,
        indexing: str = "open",
        tail_bars: int = None,
        check_for_future_data: bool = True,
    ) -> pd.DataFrame:
        """Checks the index of inputted data to ensure it contains no future
        data.

        Parameters
        ----------
        ohlc_data : pd.DataFrame
            DESCRIPTION.

        timestamp : datetime
            The current timestamp.

        indexing : str, optional
            How the OHLC data has been indexed (either by bar 'open' time, or
            bar 'close' time). The default is 'open'.

        tail_bars : int, optional
            If provided, the data will be truncated to provide the number
            of bars specified. The default is None.

        check_for_future_data : bool, optional
            A flag to check for future entries in the data. The default is True.

        Raises
        ------
        Exception
            When an unrecognised data indexing type is specified.

        Returns
        -------
        past_data : pd.DataFrame
            The checked data.

        """
        # TODO - this method is now obsolete - delete it
        if check_for_future_data:
            if indexing.lower() == "open":
                past_data = ohlc_data[ohlc_data.index < timestamp]
            elif indexing.lower() == "close":
                past_data = ohlc_data[ohlc_data.index <= timestamp]
            else:
                raise Exception(f"Unrecognised indexing type '{indexing}'.")
        else:
            past_data = ohlc_data

        if tail_bars is not None:
            past_data = past_data.tail(tail_bars)

        return past_data

    def _check_auxdata(
        self,
        auxdata: dict,
        timestamp: datetime,
        indexing: str = "open",
        tail_bars: int = None,
        check_for_future_data: bool = True,
    ) -> dict:
        """Function to check the strategy auxiliary data.

        Parameters
        ----------
        auxdata : dict
            The strategy's auxiliary data.

        timestamp : datetime
            The current timestamp.

        indexing : str, optional
            How the OHLC data has been indexed (either by bar 'open' time, or
            bar 'close' time). The default is 'open'.

        tail_bars : int, optional
            If provided, the data will be truncated to provide the number
            of bars specified. The default is None.

        check_for_future_data : bool, optional
            A flag to check for future entries in the data. The default is True.

        Returns
        -------
        dict
            The checked auxiliary data.
        """
        # TODO - this method is now obsolete - delete it
        processed_auxdata = {}
        for key, item in auxdata.items():
            if isinstance(item, pd.DataFrame) or isinstance(item, pd.Series):
                processed_auxdata[key] = self._check_ohlc_data(
                    item, timestamp, indexing, tail_bars, check_for_future_data
                )
            else:
                processed_auxdata[key] = item
        return processed_auxdata

    def _check_data(self, timestamp: datetime, indexing: str = "open") -> dict:
        """Function to return trading data based on the current timestamp. If
        dynamc_data updates are required (eg. when livetrading), the
        datastream will be refreshed each update to retrieve new data. The
        data will then be checked to ensure that there is no future data
        included.

        Parameters
        ----------
        timestamp : datetime
            Current update time.

        indexing : str, optional
            Data reference point. The default is 'open'.

        Returns
        -------
        strat_data : dict
            The checked strategy data.

        current_bars : dict(pd.core.series.Series)
            The current bars for each product.

        quote_bars : dict(pd.core.series.Series)
            The current quote data bars for each product.

        sufficient_data : bool
            Boolean flag whether sufficient data is available.

        """
        # TODO - this method is now obsolete - delete it

        def get_current_bars(
            data: pd.DataFrame,
            quote_data: bool = False,
            processed_strategy_data: dict = None,
        ) -> dict:
            """Returns the current bars of data. If the inputted data is for
            quote bars, then the quote_data boolean will be True.
            """
            if len(data) > 0:
                # TODO - call broker to get orderbook instead of bars

                current_bars = self.datastream.get_trading_bars(
                    data=data,
                    quote_bars=quote_data,
                    timestamp=timestamp,
                    processed_strategy_data=processed_strategy_data,
                )
            else:
                current_bars = None
            return current_bars

        def process_strat_data(original_strat_data, check_for_future_data):
            sufficient_data = True

            if isinstance(original_strat_data, dict):
                if "aux" in original_strat_data:
                    # Auxiliary data is being used
                    base_data = original_strat_data["base"]
                    processed_auxdata = self._check_auxdata(
                        original_strat_data["aux"],
                        timestamp,
                        indexing,
                        no_bars,
                        check_for_future_data,
                    )
                else:
                    # MTF data
                    base_data = original_strat_data

                # Process base OHLC data
                processed_basedata = {}
                if isinstance(base_data, dict):
                    # Base data is multi-timeframe; process each timeframe
                    for granularity, data in base_data.items():
                        processed_basedata[granularity] = self._check_ohlc_data(
                            data, timestamp, indexing, no_bars, check_for_future_data
                        )
                elif isinstance(base_data, pd.DataFrame) or isinstance(
                    base_data, pd.Series
                ):
                    # Base data is a timeseries already, check directly
                    processed_basedata = self._check_ohlc_data(
                        base_data, timestamp, indexing, no_bars, check_for_future_data
                    )

                # Combine the results of the conditionals above
                strat_data = {}
                if "aux" in original_strat_data:
                    strat_data["aux"] = processed_auxdata
                    strat_data["base"] = processed_basedata
                else:
                    strat_data = processed_basedata

                # Extract current bar
                first_tf_data = processed_basedata[list(processed_basedata.keys())[0]]
                current_bars = get_current_bars(
                    first_tf_data, processed_strategy_data=strat_data
                )

                # Check that enough bars have accumulated
                if len(first_tf_data) < no_bars:
                    sufficient_data = False

            elif isinstance(original_strat_data, pd.DataFrame):
                strat_data = self._check_ohlc_data(
                    original_strat_data,
                    timestamp,
                    indexing,
                    no_bars,
                    check_for_future_data,
                )
                current_bars = get_current_bars(
                    strat_data, processed_strategy_data=strat_data
                )

                # Check that enough bars have accumulated
                if len(strat_data) < no_bars:
                    sufficient_data = False

            elif original_strat_data is None:
                # Using none data
                strat_data = None
                current_bars = {}
                sufficient_data = True

            else:
                raise Exception("Unrecognised data type. Cannot process.")

            return strat_data, current_bars, sufficient_data

        # Define minimum number of bars for strategy to run
        no_bars = self._strategy_params["period"]

        if self._backtest_mode:
            check_for_future_data = True
            if self._dynamic_data:
                self._refresh_data(timestamp)
        else:
            # Livetrading
            self._refresh_data(timestamp)
            check_for_future_data = False

        strat_data, current_bars, sufficient_data = process_strat_data(
            self._strat_data, check_for_future_data
        )

        # Process quote data
        if isinstance(self.quote_data, dict):
            processed_quote_data = {}
            for instrument in self.quote_data:
                processed_quote_data[instrument] = self._check_ohlc_data(
                    self.quote_data[instrument],
                    timestamp,
                    indexing,
                    no_bars,
                    check_for_future_data,
                )
            quote_data = processed_quote_data[instrument]  # Dummy

        elif isinstance(self.quote_data, pd.DataFrame):
            quote_data = self._check_ohlc_data(
                self.quote_data, timestamp, indexing, no_bars, check_for_future_data
            )
            processed_quote_data = {self.instrument: quote_data}

        elif self.quote_data is None:
            # Using 'none' data feed
            quote_bars = current_bars
            return strat_data, current_bars, quote_bars, sufficient_data

        else:
            raise Exception("Unrecognised data type. Cannot process.")

        # Get quote bars
        quote_bars = get_current_bars(quote_data, True, processed_quote_data)

        return strat_data, current_bars, quote_bars, sufficient_data

    def _check_last_bar(self, current_bars: dict) -> bool:
        """Checks for new data to prevent duplicate signals."""
        # TODO - this method is now obsolete - delete it
        if self._allow_duplicate_bars:
            new_data = True
        else:
            try:
                duplicated_bars = []
                for product, bar in current_bars.items():
                    if (bar == self._last_bars[product]).all():
                        duplicated_bars.append(True)
                    else:
                        duplicated_bars.append(False)

                if len(duplicated_bars) == sum(duplicated_bars):
                    new_data = False
                else:
                    new_data = True

            except:
                new_data = True

        # Reset last bars
        self._last_bars = current_bars

        if not new_data and not self._backtest_mode:
            self.logger.warning("Duplicate bar detected. Skipping.")

        return new_data

    def _check_strategy_for_plot_data(self, use_strat_plot_data: bool = False):
        """Checks the bot's strategy to see if it has the plot_data attribute.

        Returns
        -------
        plot_data : pd.DataFrame
            The data to plot.

        Notes
        -----
        This method is a placeholder for a future feature, allowing
        customisation of what is plotted by setting plot_data and plot_type
        attributes from within a strategy.
        """
        strat_params = self._strategy.__dict__
        if "plot_data" in strat_params and use_strat_plot_data:
            plot_data = strat_params["plot_data"]
        else:
            plot_data = self._broker.get_candles(instrument=self.instrument)

        return plot_data

    def _strategy_shutdown(
        self,
    ):
        """Perform the strategy shutdown routines, if they exist."""
        if self._strategy_shutdown_method is not None:
            try:
                shutdown_method = getattr(
                    self._strategy, self._strategy_shutdown_method
                )
                shutdown_method()
            except AttributeError:
                self.logger.error(
                    f"\nShutdown method '{self._strategy_shutdown_method}' not found!"
                )

    def _replace_data(self, data: pd.DataFrame) -> None:
        """Function to replace the data assigned locally and to the strategy.
        Called when there is a mismatch in data lengths during multi-instrument
        backtests in periodic update mode.
        """
        self.data = data
        self._strategy.data = data

    @staticmethod
    def _submit_order(broker: Broker, order: Order, *args, **kwargs):
        "The default order execution method."
        broker.place_order(order, *args, **kwargs)
