import xmlrpc.client as xmlrpc_client
import pandas as pd
import os
import glob
import calendar


def load_from_odoo(url, db, username, password, year, month):
    print('0/4 - Download from Odoo started...')
    
    common = xmlrpc_client.ServerProxy('{}/xmlrpc/2/common'.format(url))
    uid = common.authenticate(db, username, password, {})
    print('     ... Authentication done, setting up xmlrpc client ...')
    models = xmlrpc_client.ServerProxy('{}/xmlrpc/2/object'.format(url))
    print('     ... Odoo authentication and xmlrpc client done')
    
    fields = [
        'id',
        'date',
        'employee_id',
        'duration_unit_amount',
        'task_name',
        'task_color',
        'partner_id',
        'project_type',
        'project_id',
        'date_start',
        'date_stop',
        'x_studio_titolo',
        'name',
        'write_date'
    ]
    
    last_day = calendar.monthrange(int(year), int(month))[1]
    date_from = f'{int(year)}-{int(month):02d}-01'
    date_to = f'{int(year)}-{int(month):02d}-{last_day}'

    filters = [[
        ['is_timesheet', '=', True],
        ['id', '>', 40000],
        ['date', '>=', date_from],
        ['date', '<=', date_to],
    ]]

    print("2/4 - Download from Odoo started...")
    print("\r", end='')

    items = models.execute_kw(db, uid, password, 'account.analytic.line', 'search_read', filters, {'fields': fields})
    df = pd.DataFrame.from_dict(items)

    print('     ...Database downloaded from Odoo...')
    
    ### Data cleaning
    df_ = df[df['task_name'].isin(['Assistenza','Intervento'])]
    df_ = df[df['task_color'].isin([3,5])]
    df_['date'] = pd.to_datetime(df_['date'])
    
    df_['year'] = df_['date'].apply(lambda x: x.strftime("%Y")).astype(int)
    df_['month'] = df_['date'].apply(lambda x: x.strftime("%m")).astype(int)
    
    df_ = df_[(df_['year']==int(year)) & (df_['month']==int(month))]
    df_.drop(columns=['year','month'])

    df_['employee_id'] = df_['employee_id'].apply(lambda x: str(x))
    df_['partner_id'] = df_['partner_id'].apply(lambda x: str(x))
    df_['project_id'] = df_['project_id'].replace('"',"'")
    df_ = df_[df_['employee_id'] != "[21, 'Federico Parolo - SW Line s.r.l.']"]
    print('     ...Database filtered and cleaned')
    return df_


def export_to_csv(df, export_path, export_name):
    print('3/4 - Export to csv started...')
    file_path = export_path+export_name
    print(f'     ... file_path {file_path} ...')
    os.makedirs(export_path, exist_ok=True)
    
    df.to_csv(file_path)
    print('file saved: ', file_path)
    try:
        from storage import upload_file, to_storage_key
        upload_file(file_path, to_storage_key(file_path))
        print('file uploaded to Supabase Storage')
    except Exception as e:
        print(f'Supabase upload failed (non-fatal): {e}')
    print(glob.glob('*'))
    print(glob.glob('Odoo exports/*'))
    print('3/4 - Database export on csv done')


def download_csv_from_odoo(url, db, username, password, year, month, export_path, export_name):
    print('started download_csv_from_odoo function')
    print(year, ' - ', month)
    try:
        print('started load_from_odoo function')
        df = load_from_odoo(url, db, username, password, year, month)
        print('started export_to_csv function')
        export_to_csv(df, export_path, export_name)
        message = 'Download completed!'
        print('Download completed!')
    except Exception as e:
        message = f'Download failed: {e}'
        print(f'Download failed: {e}')

    return message