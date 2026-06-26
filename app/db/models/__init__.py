"""ORM models. Import here so Alembic's autogenerate sees them."""
from app.db.models.alert import Alert
from app.db.models.capacitacion import Capacitacion
from app.db.models.cliente import Cliente
from app.db.models.dia_operativo import DiaOperativo
from app.db.models.driver import Driver
from app.db.models.driver_position import DriverPosition
from app.db.models.empresa import Empresa
from app.db.models.empresa_contacto import EmpresaContacto
from app.db.models.ruta import Ruta
from app.db.models.user import User
from app.db.models.user_empresa import UserEmpresa
from app.db.models.vehicle import Vehicle
from app.db.models.visita import Visita
from app.db.models.visita_evento import VisitaEvento

__all__ = [
    "Alert",
    "Capacitacion",
    "Cliente",
    "DiaOperativo",
    "Driver",
    "DriverPosition",
    "Empresa",
    "EmpresaContacto",
    "Ruta",
    "User",
    "UserEmpresa",
    "Vehicle",
    "Visita",
    "VisitaEvento",
]
