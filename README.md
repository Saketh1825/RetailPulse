# RetailPulse

### Retail Data Pipeline & Demand Forecasting Platform

RetailPulse is an end-to-end retail analytics backend that transforms raw sales data into a validated PostgreSQL database, provides SQL-backed analytics through REST APIs, and generates item-level demand forecasts using machine learning.

The project demonstrates a complete data workflow:

**Data Generation → ETL → Validation → PostgreSQL → Analytics → Forecasting → REST API**

> **Status:** Core ETL, database, analytics API, forecasting, testing, and GitHub setup are complete. Natural-language-to-SQL functionality is planned for a future phase.

---

## 🚀 Features

- Automated retail sales ETL pipeline
- Data validation and cleaning
- Duplicate and conflicting-record detection
- Missing-value and outlier handling
- Rejected-record tracking with machine-readable reasons
- PostgreSQL data storage
- Retail sales analytics
- Revenue-by-category analysis
- Top-product analysis
- Monthly revenue and month-over-month growth
- Item-level demand forecasting
- Forecast evaluation against baseline models
- FastAPI REST APIs
- Swagger/OpenAPI documentation
- Automated testing with pytest
- Reproducible synthetic dataset generation

---

## 🏗️ Architecture

```text
                    Raw Sales CSV
                         │
                         ▼
               ┌──────────────────┐
               │   Extract Data   │
               └────────┬─────────┘
                        │
                        ▼
               ┌──────────────────┐
               │ Validate & Clean │
               └────────┬─────────┘
                        │
              ┌─────────┴─────────┐
              │                   │
              ▼                   ▼
       Rejected Records       Clean Data
              │                   │
              ▼                   ▼
      Quality Reports       PostgreSQL
                                  │
                     ┌────────────┴────────────┐
                     │                         │
                     ▼                         ▼
               Analytics API             Forecasting
                                             │
                                             ▼
                                      Linear Regression
                                             │
                                             ▼
                                       Forecast API
🧰 Tech Stack
Category	Technology
Programming Language	Python
Data Processing	Pandas
Database	PostgreSQL
API Framework	FastAPI
Machine Learning	Scikit-learn
Forecasting Model	Linear Regression
Database Driver	psycopg
Testing	Pytest
API Documentation	Swagger / OpenAPI
Version Control	Git & GitHub
📊 ETL Pipeline

RetailPulse processes raw retail sales data through a multi-stage ETL pipeline.

1. Extract

Reads raw sales data from CSV files.

2. Validate

The pipeline checks for:

Missing values
Invalid dates
Invalid revenue
Negative quantities
Zero-unit/non-zero-revenue records
Duplicate records
Conflicting duplicates
Quantity outliers
Price outliers
3. Clean & Repair

The pipeline performs safe transformations such as:

Date-format normalization
Currency-symbol removal
Numeric unit conversion
Whitespace and case normalization
Missing-category handling
4. Reject

Invalid records are separated from valid records and stored with their rejection reason.

5. Load

Validated records are loaded into PostgreSQL.

🧹 Data Quality Results

Latest successful ETL run on the generated sample dataset:

Metric	Result
Rows read	26,437
Clean rows	25,371
Rows rejected	1,066
Rejection rate	4.03%
Items loaded	24
Sales rows loaded	25,371
Total units	1,095,944
Total revenue	2,901,538.74
Date range	2022-01-01 → 2024-12-31
Repairs performed
Item-name whitespace/case normalization
Category normalization
Date-format repair
Currency-symbol removal
Units stored as floating-point values
Missing-category handling
Rejection examples
Duplicate rows
Conflicting duplicates
Invalid dates
Missing dates
Missing item names
Missing units
Invalid units
Missing revenue
Invalid revenue
Negative units
Negative revenue
Outlier units
Outlier prices

The pipeline keeps rejected records and their reasons instead of silently dropping invalid data.

📈 Analytics

RetailPulse exposes SQL-backed analytics through FastAPI.

Available analytics
Overall sales summary
Top items by revenue
Top items by units sold
Revenue by category
Category revenue share
Monthly revenue
Month-over-month revenue growth
Latest ETL/data-quality report
Example
GET /analytics/summary

Example response:

{
  "items": 24,
  "sales_rows": 25371,
  "first_date": "2022-01-01",
  "last_date": "2024-12-31",
  "total_units": 1095944,
  "total_revenue": 2901538.74
}
Top Items Example
GET /analytics/top-items?limit=10&metric=revenue

Example results from the sample dataset:

Rank	Product	Category	Revenue
1	Sourdough Loaf	Bakery	225,887.79
2	Multigrain Bread	Bakery	205,804.05
3	Whole Milk 1L	Dairy	204,708.57
🤖 Demand Forecasting

RetailPulse generates item-level demand forecasts using Linear Regression.

The forecasting pipeline uses historical sales patterns including:

Weekly lag features
Weekday information
Trend
Annual seasonality

The model is evaluated using a chronological holdout instead of randomly splitting time-series data.

Baseline Models

The forecasting model is compared against:

Seasonal Naive
Recent Mean
Latest Training Results
Metric	Linear Regression	Seasonal Naive	Recent Mean
Macro MAE	7.6683	9.7947	10.0007
Rolling-Origin Evaluation
Windows evaluated: 92

Model MAE:             6.3308
Seasonal Naive MAE:    8.5669
Recent Mean MAE:       9.4838

Windows beating Seasonal Naive: 90 / 92

The training pipeline successfully trained models for 23 active items. One inactive item was skipped.

Forecast API
GET /forecast/{item_id}

Example:

GET /forecast/20

The API returns:

Item information
Forecast horizon
Model type
Training period
Holdout period
Holdout MAE
Holdout WAPE
Baseline metrics
Future predicted demand
Forecast limitations
Forecast Limitation

The forecast is a statistical estimate based on historical sales patterns.

It does not automatically account for:

Promotions
Stock-outs
Price changes
One-off events
Unexpected market changes

The included dataset is synthetic, so the reported accuracy demonstrates the implemented training and evaluation pipeline rather than guaranteed real-world retail accuracy.

🔌 REST API

RetailPulse is built using FastAPI.

Endpoints
Method	Endpoint	Purpose
GET	/health	Application and database health
GET	/items	List available items
GET	/analytics/summary	Overall sales metrics
GET	/analytics/top-items	Top items by revenue/units
GET	/analytics/revenue-by-category	Category revenue analysis
GET	/analytics/monthly-revenue	Monthly revenue trends
GET	/data-quality/latest	Latest ETL quality report
GET	/forecast/{item_id}	Item-level demand forecast

Interactive API documentation is available through Swagger UI:

http://127.0.0.1:8000/docs
🗄️ Database

RetailPulse uses PostgreSQL as its primary database.

Main Tables
items
sales
forecasts
forecast_models
etl_runs
Relationships
items
 ├── sales
 ├── forecasts
 └── forecast_models

etl_runs
 └── ETL execution and data-quality information

The database stores both retail data and forecasting results, allowing analytics and forecast APIs to operate from the same backend.

🧪 Testing

The project includes automated tests covering ETL, forecasting, and API functionality.

Latest verified local test run:

56 passed
38 skipped
0 failed

The skipped tests are integration tests that require a separately reachable PostgreSQL test database.

No test failures were reported in the verified run.

⚙️ Local Setup
Prerequisites
Python 3.11+
PostgreSQL
Git
1. Clone the Repository
git clone https://github.com/Saketh1825/RetailPulse.git
cd RetailPulse
2. Create a Virtual Environment
Windows
python -m venv .venv
.venv\Scripts\activate
Linux/macOS
python -m venv .venv
source .venv/bin/activate
3. Install Dependencies
pip install -r requirements.txt
pip install -r requirements-dev.txt
4. Configure Environment Variables

Create a .env file based on .env.example.

Add your local PostgreSQL connection details.

.env contains local credentials and must never be committed to GitHub.

5. Initialize the Database
python -m scripts.init_db
6. Generate Sample Data
python -m scripts.generate_sample_data

This generates a reproducible synthetic retail dataset.

7. Run the ETL Pipeline
python -m etl.pipeline --input data\raw\sales_raw.csv
8. Train Forecasting Models
python -m app.forecasting.train
9. Start the API
uvicorn app.main:app --reload

Open Swagger:

http://127.0.0.1:8000/docs
📁 Project Structure
retailpulse/
│
├── app/
│   ├── forecasting/
│   ├── routers/
│   ├── db.py
│   ├── models.py
│   └── main.py
│
├── data/
│   ├── raw/
│   └── rejected/
│
├── db/
│   ├── schema.sql
│   └── grants.sql
│
├── etl/
│   ├── clean.py
│   ├── extract.py
│   ├── load.py
│   ├── pipeline.py
│   └── transform.py
│
├── scripts/
│   ├── generate_sample_data.py
│   └── init_db.py
│
├── tests/
│
├── artifacts/
│   └── metrics.json
│
├── docs/
│   └── ARCHITECTURE.md
│
├── .env.example
├── .gitignore
├── pyproject.toml
├── pytest.ini
├── requirements.txt
└── requirements-dev.txt
🔐 Security & Configuration

Sensitive configuration is excluded from version control.

The project ignores:

.env
*.pem
.venv/
__pycache__/
.pytest_cache/

Database credentials and API keys should always be stored in environment variables.

📌 Project Status
Completed
 PostgreSQL database setup
 Synthetic data generation
 ETL pipeline
 Data validation and cleaning
 Rejected-record handling
 Analytics APIs
 Data-quality API
 Forecasting pipeline
 Forecast API
 Automated tests
 GitHub repository
Planned
 Natural-language-to-SQL interface
 Production deployment
 Dockerization
 Hosted PostgreSQL
 Optional analytics dashboard
 CI/CD automation
🛣️ Future Improvements

Potential future improvements include:

Natural-language-to-SQL with strict read-only database controls
Docker-based deployment
Hosted PostgreSQL
Automated CI/CD testing
Interactive analytics dashboard
Additional forecasting approaches
Evaluation using real-world retail datasets
Production monitoring and observability
🎯 Design Philosophy

RetailPulse intentionally avoids unnecessary infrastructure and complexity.

The project does not depend on technologies such as Spark, Kafka, Airflow, Kubernetes, or multi-agent frameworks.

The goal is to build a system whose complete data flow can be understood, tested, explained, and defended end-to-end.

👨‍💻 Author

Saketh Goudi

B.Tech — Computer Science (Data Science)

GitHub: @Saketh1825

📄 License

This project is currently intended as a portfolio and educational project.


**This is the one to paste.** After saving it as `README.md`, don't change anything else yet.
