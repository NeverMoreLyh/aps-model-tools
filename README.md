# APS Model Tools

Read-only APS metadata scanner, SQLite relationship index, impact query, and Table-to-DDL preview.

```bash
python3 -m aps_model_tools scan --workspace /Users/joshua/code/v8.7-all --db .data/v87.db
python3 -m aps_model_tools stats --db .data/v87.db
python3 -m aps_model_tools show ApBaseType.U_STD_DT_TP --db .data/v87.db
python3 -m aps_model_tools refs ApBaseType.U_STD_DT_TP --direction both --depth 2 --db .data/v87.db
python3 -m aps_model_tools impact BpDict.A.addr --db .data/v87.db
python3 -m aps_model_tools ddl SysSMSTables.kapp_shrt_mesg_ntc --dialect mysql --db .data/v87.db
```

The tool never connects to a database or modifies APS business repositories.
