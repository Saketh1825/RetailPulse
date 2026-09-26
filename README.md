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
