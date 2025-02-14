# Entity Relationship Diagram (ERD)

```mermaid
erDiagram
    CUSTOMER ||--o{ ORDER : places
    ORDER ||--|{ ITEM : contains
    CUSTOMER {
        int id
        string name
    }
    ORDER {
        int id
        date order_date
    }
    ITEM {
        int id
        string name
    }
