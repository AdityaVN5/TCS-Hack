import csv
import io
import zipfile
from pathlib import Path
from openpyxl import load_workbook

MAX_BYTES = 20 * 1024 * 1024
MAX_ROWS = 100_000

def parse(data: bytes, filename: str):
    if len(data) > MAX_BYTES:
        raise ValueError('Each file must be 20 MB or smaller.')
    suffix = Path(filename).suffix.lower()
    if suffix == '.csv':
        try:
            reader = csv.reader(io.StringIO(data.decode('utf-8-sig')))
            header = next(reader)
            rows = []
            for row in reader:
                if len(rows) >= MAX_ROWS:
                    raise ValueError('A file may contain at most 100,000 rows.')
                if any(row):
                    rows.append(row)
        except (UnicodeDecodeError, StopIteration, csv.Error) as e:
            raise ValueError('Upload a nonempty UTF-8 CSV with a header row.') from e
        return [sheet('CSV',header,rows)]
    if suffix != '.xlsx':
        raise ValueError('Only CSV and XLSX files are supported. Macro-enabled files are rejected.')
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if sum(i.file_size for i in z.infolist()) > 200 * 1024 * 1024:
                raise ValueError('Expanded workbook exceeds 200 MB.')
            if any('vbaproject' in n.lower() or 'externallinks/' in n.lower() for n in z.namelist()):
                raise ValueError('Macros and external workbook links are not allowed.')
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        formulas = load_workbook(io.BytesIO(data), read_only=True, data_only=False)
        try:
            if len(wb.sheetnames) > 20:
                raise ValueError('A workbook may contain at most 20 sheets.')
            result, count = [], 0
            for ws in wb:
                values = ws.iter_rows(values_only=True)
                header = next(values, None)
                if header is None:
                    continue
                rows, missing = [], []
                formula_rows = formulas[ws.title].iter_rows(values_only=True)
                next(formula_rows, None)
                for index, (row, formula_row) in enumerate(zip(values, formula_rows), 2):
                    count += 1
                    if count > MAX_ROWS:
                        raise ValueError('Workbook exceeds 100,000 total rows.')
                    for col, val in enumerate(formula_row):
                        if isinstance(val,str) and val.startswith('=') and row[col] is None:
                            missing.append({'row':index,'column':str(header[col])})
                    rows.append([v.isoformat() if hasattr(v,'isoformat') else v for v in row])
                item = sheet(ws.title,header,rows)
                item['missing_formula_values'] = missing
                result.append(item)
            if not result:
                raise ValueError('The workbook contains no data sheets.')
            return result
        finally:
            wb.close()
            formulas.close()
    except (zipfile.BadZipFile, KeyError) as e:
        raise ValueError('This XLSX workbook is damaged or invalid.') from e

def sheet(name, header, rows):
    headers = [str(h).strip() if h is not None else '' for h in header]
    if not all(headers) or len(headers) != len(set(headers)):
        raise ValueError('Every column needs a unique, nonempty header.')
    if any(len(r)>len(headers) for r in rows):
        raise ValueError('A row contains more values than the header.')
    return {'name':name,'headers':headers,'rows':[dict(zip(headers,r)) for r in rows], 'row_count':len(rows),'missing_formula_values':[]}
