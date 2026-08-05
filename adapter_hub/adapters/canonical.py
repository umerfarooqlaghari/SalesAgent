from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

class CanonicalBaseModel(BaseModel):
    """Base model with shared fields for canonical entities."""
    sync_timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    raw_metadata: Dict[str, Any] = Field(default_factory=dict, description="Original un-normalized fields for backup/extensibility")

class Product(CanonicalBaseModel):
    id: str = Field(..., description="Unique product ID or SKU")
    name: str = Field(..., description="Name of the product")
    price: float = Field(default=0.0, description="Product price")
    currency: str = Field(default="USD", description="Currency code")
    stock_quantity: int = Field(default=0, description="In-stock quantity")
    description: Optional[str] = Field(None, description="Detailed description")
    category: Optional[str] = Field(None, description="Product category")

class Customer(CanonicalBaseModel):
    id: str = Field(..., description="Unique customer ID")
    name: str = Field(..., description="Full name or company representative name")
    email: str = Field(..., description="Primary email address")
    phone: Optional[str] = Field(None, description="Phone number")
    company: Optional[str] = Field(None, description="Company name")
    status: str = Field(default="active", description="Lead/Customer status (e.g., Lead, Active, Churned)")

class OrderItem(BaseModel):
    product_id: Optional[str] = None
    product_name: str
    quantity: int = 1
    unit_price: float = 0.0

class Order(CanonicalBaseModel):
    id: str = Field(..., description="Unique order ID")
    customer_id: Optional[str] = Field(None, description="Associated customer ID")
    customer_email: str = Field(..., description="Verification customer email")
    customer_phone: Optional[str] = Field(None, description="Verification customer phone")
    status: str = Field(..., description="Current state (e.g., Pending, Processing, Shipped, Cancelled)")
    total_price: float = Field(0.0, description="Grand total price")
    items: List[OrderItem] = Field(default_factory=list, description="List of items in the order")

class Log(CanonicalBaseModel):
    id: str = Field(..., description="Unique log ID")
    tenant_id: str = Field(..., description="Scoping tenant ID")
    agent_id: str = Field(..., description="Scoping agent ID")
    action: str = Field(..., description="Performed action (e.g., Sync, Search, Introspect)")
    details: str = Field(..., description="Log description or payload")
    status: str = Field(default="SUCCESS", description="SUCCESS, WARNING, or ERROR")


class Record(CanonicalBaseModel):
    """Generic row from custom tables (productions, sets, POs, etc.)."""
    id: str = Field(..., description="Unique record ID")
    entity_label: str = Field(..., description="Human label for the source table")
    table_name: str = Field(..., description="Source table name")
    summary: str = Field(..., description="Searchable text summary of the row")
    fields: Dict[str, Any] = Field(default_factory=dict)
