#!/bin/sh
#SBATCH --job-name=r2
#SBATCH --output=/s/b/r2.driver.log
#SBATCH --nodes=1
#SBATCH --cpus-per-task=1
#SBATCH --export=NONE
#SBATCH --no-requeue
exec /s/bin/edr_driver-ab.py /s/b/r2.spec.json
