"""Regiones y comunas de Chile — ISO 3166-2:CL.

Static reference data. No DB table needed.
"""
from __future__ import annotations

REGIONES: list[dict[str, str | list[str]]] = [
    {"codigo": "CL-AP", "nombre": "Arica y Parinacota", "comunas": ["Arica", "Camarones", "General Lagos", "Putre"]},
    {"codigo": "CL-TA", "nombre": "Tarapaca", "comunas": ["Alto Hospicio", "Camina", "Colchane", "Huara", "Iquique", "Pica", "Pozo Almonte"]},
    {"codigo": "CL-AN", "nombre": "Antofagasta", "comunas": ["Antofagasta", "Calama", "Maria Elena", "Mejillones", "Olague", "San Pedro de Atacama", "Sierra Gorda", "Taltal", "Tocopilla"]},
    {"codigo": "CL-AT", "nombre": "Atacama", "comunas": ["Caldera", "Chanaral", "Copiapo", "Diego de Almagro", "Freirina", "Huasco", "Tierra Amarilla", "Vallenar", "Alto del Carmen"]},
    {"codigo": "CL-CO", "nombre": "Coquimbo", "comunas": ["Andacollo", "Canela", "Combarbala", "Coquimbo", "Illapel", "La Higuera", "La Serena", "Los Vilos", "Monte Patria", "Ovalle", "Paiguano", "Punitaqui", "Rio Hurtado", "Salamanca", "Vicuna"]},
    {"codigo": "CL-VS", "nombre": "Valparaiso", "comunas": ["Algarrobo", "Cabildo", "Calera", "Calle Larga", "Cartagena", "Casablanca", "Catemu", "Concon", "El Quisco", "El Tabo", "Hijuelas", "Isla de Pascua", "Juan Fernandez", "La Cruz", "La Ligua", "Limache", "Llaillay", "Los Andes", "Nogales", "Olmue", "Panquehue", "Papudo", "Petorca", "Puchuncavi", "Putaendo", "Quillota", "Quilpue", "Quintero", "Rinconada", "San Antonio", "San Esteban", "San Felipe", "Santa Maria", "Santo Domingo", "Valparaiso", "Villa Alemana", "Vina del Mar", "Zapallar"]},
    {"codigo": "CL-RM", "nombre": "Metropolitana de Santiago", "comunas": ["Alhue", "Buin", "Calera de Tango", "Cerrillos", "Cerro Navia", "Colina", "Conchali", "Curacavi", "El Bosque", "El Monte", "Estacion Central", "Huechuraba", "Independencia", "Isla de Maipo", "La Cisterna", "La Florida", "La Granja", "La Pintana", "La Reina", "Lampa", "Las Condes", "Lo Barnechea", "Lo Espejo", "Lo Prado", "Macul", "Maipu", "Maria Pinto", "Melipilla", "Nunoa", "Padre Hurtado", "Paine", "Pedro Aguirre Cerda", "Penalolen", "Penaflor", "Pirque", "Providencia", "Pudahuel", "Puente Alto", "Quilicura", "Quinta Normal", "Recoleta", "Renca", "San Bernardo", "San Joaquin", "San Jose de Maipo", "San Miguel", "San Pedro", "San Ramon", "Santiago", "Talagante", "Tiltil", "Vitacura"]},
    {"codigo": "CL-LI", "nombre": "O'Higgins", "comunas": ["Codegua", "Coinco", "Coltauco", "Donihue", "Graneros", "Las Cabras", "Machali", "Malloa", "Mostazal", "Nancagua", "Olivar", "Peumo", "Pichidegua", "Pichilemu", "Quinta de Tilcoco", "Rancagua", "Rengo", "Requinoa", "San Fernando", "San Vicente", "Santa Cruz"]},
    {"codigo": "CL-ML", "nombre": "Maule", "comunas": ["Cauquenes", "Chanco", "Colbun", "Constitucion", "Curepto", "Curico", "Empedrado", "Hualane", "Licanten", "Linares", "Longavi", "Maule", "Molina", "Parral", "Pelarco", "Pelluhue", "Pencahue", "Rauco", "Retiro", "Rio Claro", "Romeral", "Sagrada Familia", "San Clemente", "San Javier", "San Rafael", "Talca", "Teno", "Vichuquen", "Villa Alegre", "Yerbas Buenas"]},
    {"codigo": "CL-NB", "nombre": "Nuble", "comunas": ["Bulnes", "Chillan", "Chillan Viejo", "Cobquecura", "Coelemu", "Coihueco", "El Carmen", "Ninhue", "Niquen", "Pemuco", "Pinto", "Portezuelo", "Quillon", "Quirihue", "Ranquil", "San Carlos", "San Fabian", "San Ignacio", "San Nicolas", "Trehuaco", "Yungay"]},
    {"codigo": "CL-BI", "nombre": "Biobio", "comunas": ["Alto Biobio", "Antuco", "Arauco", "Cabrero", "Canete", "Chiguayante", "Concepcion", "Contulmo", "Coronel", "Curanilahue", "Florida", "Hualpen", "Hualqui", "Laja", "Lebu", "Los Alamos", "Los Angeles", "Lota", "Mulchen", "Nacimiento", "Negrete", "Penco", "Quilaco", "Quilleco", "San Pedro de la Paz", "San Rosendo", "Santa Barbara", "Santa Juana", "Talcahuano", "Tirua", "Tome", "Tucapel", "Yumbel"]},
    {"codigo": "CL-AR", "nombre": "Araucania", "comunas": ["Angol", "Carahue", "Cholchol", "Collipulli", "Cunco", "Curacautin", "Curarrehue", "Ercilla", "Freire", "Galvarino", "Gorbea", "Lautaro", "Loncoche", "Lonquimay", "Los Sauces", "Lumaco", "Melipeuco", "Nueva Imperial", "Padre Las Casas", "Perquenco", "Pitrufquen", "Pucon", "Puren", "Renaico", "Saavedra", "Temuco", "Teodoro Schmidt", "Tolten", "Traiguen", "Victoria", "Vilcun", "Villarrica"]},
    {"codigo": "CL-LR", "nombre": "Los Rios", "comunas": ["Corral", "Futrono", "La Union", "Lago Ranco", "Lanco", "Los Lagos", "Mafil", "Mariquina", "Paillaco", "Panguipulli", "Rio Bueno", "Valdivia"]},
    {"codigo": "CL-LL", "nombre": "Los Lagos", "comunas": ["Ancud", "Calbuco", "Castro", "Chaiten", "Chonchi", "Cochamo", "Curaco de Velez", "Dalcahue", "Fresia", "Frutillar", "Futaleufu", "Hualaihue", "Llanquihue", "Los Muermos", "Maullin", "Osorno", "Palena", "Puerto Montt", "Puerto Octay", "Puerto Varas", "Puqueldon", "Purranque", "Puyehue", "Queilen", "Quellon", "Quemchi", "Quinchao", "Rio Negro", "San Juan de la Costa", "San Pablo"]},
    {"codigo": "CL-AI", "nombre": "Aysen", "comunas": ["Aysen", "Chile Chico", "Cisnes", "Cochrane", "Coyhaique", "Guaitecas", "Lago Verde", "O'Higgins", "Rio Ibanez", "Tortel"]},
    {"codigo": "CL-MA", "nombre": "Magallanes", "comunas": ["Antartica", "Cabo de Hornos", "Laguna Blanca", "Natales", "Porvenir", "Primavera", "Punta Arenas", "Rio Verde", "San Gregorio", "Timaukel", "Torres del Paine"]},
]


def get_regiones() -> list[dict[str, str | list[str]]]:
    return REGIONES


def get_comunas_by_region(region_nombre: str) -> list[str]:
    for r in REGIONES:
        if r["nombre"] == region_nombre:
            return list(r["comunas"])  # type: ignore[arg-type]
    return []
