# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## ⚠️ CRITICAL WARNING ⚠️

**THIS IS A LIVE TRADING SERVER** - The system actively trades real money in financial markets. Exercise extreme caution with any changes:

- **Test thoroughly** before making any modifications to production code
- **Avoid changes** to core trading logic, execution systems, or broker interfaces unless absolutely necessary
- **Always run tests** before committing changes (`python -m pytest`)
- **Consider market hours** - system may be actively trading during business hours
- **Backup critical data** before making structural changes to data handling code

## Project Overview

**pysystemtrade** is a systematic futures trading framework implementing the methodology from Rob Carver's book "Systematic Trading". It provides both backtesting capabilities and live automated trading functionality through Interactive Brokers.

## Development Commands

### Testing
```bash
# Run all tests (configured in pyproject.toml)
python -m pytest

# Run specific test paths
python -m pytest syscore/tests
python -m pytest systems/tests

# Run with coverage
coverage run -m nose
coverage report

# Run tests with tox
tox
```

### Code Quality
```bash
# Format code with black
python -m black .

# Run linting (flake8 tests are in tests/test_static.py)
python -m pytest tests/test_static.py::test_flake8

# Pre-commit hooks (black formatting)
pre-commit run --all-files
```

### Installation
```bash
# Install in development mode with dev dependencies
python -m pip install --editable '.[dev]'

# Standard installation
python -m pip install .
```

## Architecture Overview

The codebase follows a modular architecture with clear separation of concerns:

### Core Systems (`sys*` modules)
- **syscore**: Core utilities (date handling, file operations, mathematical functions)
- **sysdata**: Data access layer with multiple backends (CSV, MongoDB, Parquet, Arctic)
- **sysobjects**: Domain objects (instruments, contracts, prices, rolls, fills)
- **systems**: Trading system components (forecasting, portfolio, risk, accounts)
- **sysexecution**: Order execution and trade management
- **sysproduction**: Live trading processes and automation
- **sysbrokers**: Broker interfaces (primarily Interactive Brokers via ib_async)

### Key Design Patterns
- **Data abstraction**: Uniform interfaces for different data sources (CSV for backtesting, MongoDB for production)
- **System stages**: Modular pipeline from raw data → forecasts → positions → orders → execution
- **Configuration-driven**: YAML configuration files control system behavior
- **Process-based**: Production system runs as separate processes managed by syscontrol

### Data Flow
1. **Raw data** (prices, volumes) → **sysdata** layer
2. **Trading rules** generate forecasts → **systems.forecasting**
3. **Forecast combination** → **systems.forecast_combine**
4. **Position sizing** → **systems.positionsizing**
5. **Portfolio construction** → **systems.portfolio**
6. **Risk overlay** → **systems.risk_overlay**
7. **Order generation** → **sysexecution**
8. **Broker execution** → **sysbrokers**

### Configuration and Data
- **Configuration**: YAML files in `sysdata/config/`, `syscontrol/`, and `systems/provided/`
- **CSV data**: Historical data in `data/` for backtesting
- **Private configs**: User-specific settings in `private/` (gitignored)

### Production Architecture
- **Process management**: `syscontrol` coordinates multiple processes
- **Data updates**: Separate processes for price updates, FX updates, capital updates
- **Order management**: Stack-based order processing with multiple execution strategies
- **Monitoring**: Comprehensive logging and reporting system

## Testing Structure

Tests are organized by module with the main test directories:
- `syscore/tests/` - Core functionality tests
- `sysdata/tests/` - Data layer tests
- `systems/tests/` - Trading system tests
- `sysobjects/tests/` - Domain object tests
- `tests/` - Integration and static analysis tests

## Key Dependencies

- **pandas**: Time series data manipulation
- **numpy/scipy**: Numerical computing
- **ib_async**: Interactive Brokers API (community-maintained fork of ib-insync)
- **pymongo**: MongoDB integration
- **PyYAML**: Configuration management
- **matplotlib**: Plotting and visualization
- **pyarrow**: Parquet data format support

## Development Notes

- Python 3.10+ required
- Uses black for code formatting (line length 88)
- Pre-commit hooks enforce formatting
- Git workflow: develop branch for active development, master for releases
- Configuration files use YAML format with environment variable substitution