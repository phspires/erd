#!/usr/bin/env python3
from pathlib import Path
import psycopg2
from psycopg2 import sql
import os
import re
from collections import defaultdict
from typing import Dict, List, Tuple, Union
from dotenv import load_dotenv

class ERDGenerator:
    def __init__(self, config: Dict):
        self.config = config
        self.excluded_schemas = {'public', 'information_schema', 'pg_catalog', 'metadata'}
        self.excluded_dbs = {'rdsadmin', 'postgres'}
        self.connection = None
        self.admin_db = 'postgres'
        
        # Advanced Type Mapping Configuration
        self.type_mappings = {
            r'servicevalidation\.\w+': 'string',
            'timestamp_with_time_zone': 'timestamp',
            'timestamp without time zone': 'timestamp',
            'uuid\[\]': 'uuid_array',
            'timestamp with time zone': 'timestamptz',
            'timestamp without time zone': 'timestamp',
            'character varying': 'varchar',
            'time with time zone': 'timetz',
            'time without time zone': 'time'
        }

        script_dir = Path(__file__).parent.resolve()  # Directory of the script

        # Set the output directory to be the 'docs' folder, located next to 'scripts'
        self.output_base = script_dir.parent / 'docs' / 'diagrams'
        
        if not self.output_base.exists():
            self.output_base = script_dir.parent / 'erd_diagrams'  # Fallback in case 'docs' doesn't exist
        else:
            self.output_base = Path(self.output_base)

    def connect(self, dbname: str = None):
        """Establish database connection"""

        if dbname:
            self.config['dbname'] = dbname
        else:
            self.config['dbname'] = self.admin_db        

        self.connection = psycopg2.connect(**self.config)
        return self.connection

    def normalize_column_type(self, column_type: str) -> str:
        """
        Normalize column types for Mermaid compatibility using regex substitutions
        """
        # Remove schema prefixes (e.g., 'servicevalidation.')
        normalized = re.sub(r'^[a-z_]+\.', '', column_type)
        
        # Apply type mappings
        for pattern, replacement in self.type_mappings.items():
            normalized = re.sub(pattern, replacement, normalized)
            
        # Simplify array notations
        normalized = normalized.replace('[]', '_array')
        
        # Remove length constraints (e.g., varchar(255))
        normalized = re.sub(r'\([^)]*\)', '', normalized)
        
        return normalized.strip().lower()
    
    def get_all_databases(self) -> List[str]:
        """Fetch all non-system databases from RDS instance"""
        conn = psycopg2.connect(**{**self.config, 'dbname': 'postgres'})
        try:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT datname 
                    FROM pg_database 
                    WHERE datname NOT IN %s
                    AND datname NOT LIKE 'template%'
                    ORDER BY datname;
                """, (tuple(self.excluded_dbs),))
                return [row[0] for row in cursor.fetchall()]
        finally:
            conn.close()    

    def get_all_schemas(self) -> List[str]:
        """Fetch all non-system schemas from the database"""
        with self.connection.cursor() as cursor:
            cursor.execute("""
                SELECT schema_name 
                FROM information_schema.schemata 
                WHERE schema_name NOT IN %s
                ORDER BY schema_name;
            """, (tuple(self.excluded_schemas),))
            return [row[0] for row in cursor.fetchall()]

    def get_table_info(self, schema: str) -> Tuple[List, List]:
        """Fetch table structure and relationships for a schema"""
        with self.connection.cursor() as cursor:
            # Get tables and columns with detailed attributes
            cursor.execute("""
                SELECT
                    c.relname AS table_name,
                    a.attname AS column_name,
                    format_type(a.atttypid, a.atttypmod) AS column_type,
                    a.attnotnull AS not_null,
                    pg_get_expr(ad.adbin, ad.adrelid) AS default_value,
                    COALESCE(pk.is_pk, FALSE) AS is_pk,
                    COALESCE(uniq.is_unique, FALSE) AS is_unique,
                    col_description(c.oid, a.attnum) AS description
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                LEFT JOIN pg_attribute a ON c.oid = a.attrelid AND a.attnum > 0 AND NOT a.attisdropped
                LEFT JOIN pg_attrdef ad ON a.attrelid = ad.adrelid AND a.attnum = ad.adnum
                LEFT JOIN (
                    SELECT indrelid, attnum, TRUE AS is_pk
                    FROM pg_index i
                    JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
                    WHERE i.indisprimary
                ) pk ON pk.indrelid = c.oid AND pk.attnum = a.attnum
                LEFT JOIN (
                    SELECT indrelid, attnum, TRUE AS is_unique
                    FROM pg_index i
                    JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
                    WHERE i.indisunique AND NOT i.indisprimary
                ) uniq ON uniq.indrelid = c.oid AND uniq.attnum = a.attnum
                WHERE c.relkind = 'r'
                AND NOT c.relispartition
                AND n.nspname = %s
                ORDER BY c.relname, a.attnum;
            """, (schema,))
            tables = cursor.fetchall()

            # Get foreign key relationships
            cursor.execute("""
                SELECT
                    con.conname AS constraint_name,
                    src.relname AS source_table,
                    src_attr.attname AS source_column,
                    dest.relname AS target_table,
                    dest_attr.attname AS target_column,
                    CASE 
                        WHEN con.confrelid = con.conrelid THEN 'self' 
                        WHEN n2.nspname != n.nspname THEN 'cross_schema'
                        ELSE 'normal'
                    END AS relationship_type,
                    pg_get_constraintdef(con.oid) AS constraint_def
                FROM pg_constraint con
                JOIN pg_namespace n ON n.oid = con.connamespace
                JOIN pg_class src ON src.oid = con.conrelid
                JOIN pg_class dest ON dest.oid = con.confrelid
                JOIN pg_attribute src_attr ON src_attr.attrelid = con.conrelid AND src_attr.attnum = con.conkey[1]
                JOIN pg_attribute dest_attr ON dest_attr.attrelid = con.confrelid AND dest_attr.attnum = con.confkey[1]
                LEFT JOIN pg_namespace n2 ON n2.oid = dest.relnamespace
                WHERE n.nspname = %s
                AND con.contype = 'f'
                ORDER BY source_table, target_table;
            """, (schema,))
            foreign_keys = cursor.fetchall()

            return tables, foreign_keys

    def _save_diagram(self, schema: str, db_path: Path, diagram: str) -> None:
        """Save diagram to file with proper path handling"""
        filename = db_path / f"{schema}_erd.mmd"
        filename.write_text(diagram, encoding='utf-8')
        print(f"Saved: {filename.relative_to(self.output_base.parent)}")

    def _resolve_output_dir(self, output_dir: Union[str, Path, None]) -> Path:
        """Ensure output directory is a Path object"""
        if output_dir is None:
            return self.output_base
        return Path(output_dir) if isinstance(output_dir, str) else output_dir

    def _build_column_definition(self, column: str, pg_type: str, is_pk: bool, 
                            is_unique: bool, not_null: bool, 
                            default_val: str, description: str) -> str:
        """Construct a column definition line with metadata"""
        col_def = f"\t\t\t{column} {self.normalize_column_type(pg_type)}"
        if is_pk: col_def += " PK"
        
        constraints = []
        if is_unique: constraints.append("UK")
        if not_null: constraints.append("NOT NULL")
        if default_val: constraints.append(f"DEFAULT {default_val}")
        if description: constraints.append(f"Description: {description[:50]}...")
        
        if constraints:
            col_def += f" \"{', '.join(constraints)}\""
        return col_def

    def _build_relationship(self, src_table: str, tgt_table: str, 
                        src_col: str, tgt_col: str,
                        conname: str, rel_type: str, 
                        constraint_def: str) -> str:
        """Construct relationship line with enhanced info"""
        # Clean constraint definition
        constraint_def = re.sub(r'FOREIGN KEY .* REFERENCES', '→', constraint_def)
        
        if rel_type == 'self':
            return f"    {src_table} ||--|| {tgt_table} : \"Self-reference ({conname})\""
        elif rel_type == 'cross_schema':
            return f"    {src_table} }}o--|| {tgt_table} : \"Cross-schema: {constraint_def}\""
        else:
            return f"    {src_table} ||--o{{ {tgt_table} : \"{src_col} → {tgt_col} ({conname})\""

    def _generate_diagram(self, schema: str, tables: List, foreign_keys: List) -> str:
        """Generate diagram with improved FK visualization"""
        diagram = [f"%% ER Diagram for {schema}\nerDiagram"]
        
        # Process tables
        table_dict = defaultdict(list)
        for table, column, pg_type, not_null, default_val, is_pk, is_unique, description in tables:
            col_def = self._build_column_definition(column, pg_type, is_pk, is_unique, not_null, default_val, description)
            table_dict[table].append(col_def)
        
        # Add tables
        for table, columns in sorted(table_dict.items()):
            diagram.append(f"\t\t{table} {{")
            diagram.extend(columns)
            diagram.append("    }\n")
        
        # Add enhanced relationships
        for conname, src_table, src_col, tgt_table, tgt_col, rel_type, constraint_def in foreign_keys:
            rel_line = self._build_relationship(
                src_table, tgt_table, src_col, tgt_col, 
                conname, rel_type, constraint_def
            )
            diagram.append(rel_line)
        
        return "\n".join(diagram)

    def _process_schema(self, schema: str, db_path: Path) -> None:
        """Process a single schema"""
        try:
            tables, foreign_keys = self.get_table_info(schema)
            if not tables:
                print(f"Skipping empty schema: {schema}")
                return

            diagram = self._generate_diagram(schema, tables, foreign_keys)
            self._save_diagram(schema, db_path, diagram)
        except Exception as e:
            print(f"Error in schema {schema}: {str(e)}")

    def _process_database(self, dbname: str, db_path: Path) -> None:
        """Process a single database"""
        db_path.mkdir(exist_ok=True, parents=True)
        print(f"\nProcessing database: {dbname}")
        
        try:
            with self.connect(dbname) as conn:
                self.connection = conn
                for schema in self.get_all_schemas():
                    self._process_schema(schema, db_path)
        except Exception as e:
            print(f"Database connection failed for {dbname}: {str(e)}")
        finally:
            self.connection = None

    def generate_all_diagrams(self, output_dir: Union[str, Path, None] = None, databases: List[str] = None):
        """Generate diagrams for specified databases and schemas"""
        
        output_dir = self._resolve_output_dir(output_dir)

        os.makedirs(output_dir, exist_ok=True)
        
        # Get databases to process
        databases = databases or self.get_all_databases()
        print(f"Processing databases: {', '.join(databases)}")

        for dbname in databases:
            db_path = output_dir / dbname
            self._process_database(dbname, db_path)

if __name__ == '__main__':
    
    load_dotenv()

    config = {
        'dbname': os.getenv('PG_DBNAME'),
        'user': os.getenv('PG_USER'),
        'host': os.getenv('PG_HOST'),
        'password': os.getenv('PG_PASSWORD'),
        'port': os.getenv('PG_PORT')
    }
    
    generator = ERDGenerator(config)

    databases_to_process = [
        'prod_tiko',
        'prod_tiko_connect_brainiac',
        'prod_tiko_connect_crm_bridge',
        'prod_tiko_facial_recognition',
        'prod_tiko_feedback',
        'prod_tiko_fraud',
        'prod_tiko_loyalty_budget',
        'prod_tiko_loyalty_cashouts',
        'prod_tiko_loyalty_entities',
        'prod_tiko_loyalty_incentives',
        'prod_tiko_loyalty_ledger',
        'prod_tiko_loyalty_perks',
        'prod_tiko_loyalty_reporter',
        'prod_tiko_loyalty_spending',
        'prod_tiko_message_gateway',
        'prod_tiko_nudge',
        'prod_tiko_nudge_announcements',
        'prod_tiko_nudge_lottery',
        'prod_tiko_nudge_reminders',
        'prod_tiko_nudge_trials',
        'prod_tiko_offer_validation_decider',
        'prod_tiko_opportunity_cerebro',
        'prod_tiko_platform_file_storer',
        'prod_tiko_platform_mobile_authenticator',
        'prod_tiko_platform_mobile_inbox',
        'prod_tiko_platform_scheduler',
        'prod_tiko_platform_token',
        'prod_tiko_platform_voice_biometrics',
        'prod_tiko_product_basket',
        'prod_tiko_rafiki_card',
        'prod_tiko_rafiki_enrolment',
        'prod_tiko_service_validation',
        'prod_tiko_trust_case_management',
        'prod_tiko_trust_status_tracker'        
        ]
    generator.generate_all_diagrams(databases=databases_to_process)