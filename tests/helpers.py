from analytics_agent.models import TABLE_IDS
from analytics_agent.schema import ColumnSchema, SchemaCatalog, TableSchema


TABLE_ID = TABLE_IDS["th_primas_final"]


def sample_catalog():
    table = TableSchema(
        sheet_name="th_primas_final",
        table_id=TABLE_ID,
        columns={
            "fecha_emision": ColumnSchema("fecha_emision", "DATE", "Fecha de emisión"),
            "vrprima": ColumnSchema("vrprima", "FLOAT", "Prima emitida"),
            "cod_sucursal": ColumnSchema("cod_sucursal", "INTEGER", "Código de sucursal"),
            "nombre_ramo_comercial": ColumnSchema("nombre_ramo_comercial", "STRING", "Ramo comercial"),
            "nom_tomador": ColumnSchema("nom_tomador", "STRING", "Nombre del tomador"),
            "numero_documento": ColumnSchema("numero_documento", "STRING", "Documento del tomador"),
        },
    )
    return SchemaCatalog(tables={TABLE_ID.lower(): table})
