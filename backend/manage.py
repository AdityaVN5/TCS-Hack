"""Local workspace management; retain raw files and sealed snapshots."""
import argparse
from main import Demo, demo, mapping, Mapping, reset, assert_idle
from store import init, workspace, all_items
from worker import enqueue
from contracts import CONTRACTS

def main():
    parser=argparse.ArgumentParser(description='Manage the local TRABO demo workspace.')
    commands=parser.add_subparsers(dest='command',required=True)
    seed=commands.add_parser('seed',help='Load and map synthetic inputs, then queue validation.')
    seed.add_argument('--mode',choices=['clean','defective'],default='clean')
    reset_command=commands.add_parser('reset',help='Clear draft inputs while retaining immutable history.')
    reset_command.add_argument('--yes',action='store_true')
    args=parser.parse_args()
    init();workspace();assert_idle('demo')
    if args.command=='reset':
        if not args.yes:parser.error('Reset requires --yes. Raw originals and sealed snapshots will be preserved.')
        reset('demo')
        print('Draft inputs reset. Original files and sealed snapshots retained.')
        return
    demo(Demo(mode=args.mode),'demo')
    for upload in all_items('upload'):
        role=upload['name'].removesuffix('.csv')
        contract=CONTRACTS[role]
        for sheet in upload['sheets']:
            value=Mapping(sheet=sheet['name'],role=role,columns={k:k for k in contract['required']+contract['optional'] if k in sheet['headers']})
            mapping(upload['id'],value,'demo',{'email':'local-seed-command'})
    job=enqueue('demo','validate','local-seed-command')
    print(f"Loaded {args.mode} fixture collection. Validation job {job['id']} queued; run the worker to process it.")

if __name__=='__main__':main()
