#!/bin/sh
#SBATCH --job-name=r1
#SBATCH --output=/s/b/r1.driver.log
#SBATCH --nodes=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=8192M
#SBATCH --time=2:30:00
#SBATCH --licenses=fc:1,vcs:2
#SBATCH --nodelist=node7
#SBATCH --partition=long
#SBATCH --export=NONE
#SBATCH --no-requeue
#SBATCH --comment=a
exec /s/bin/edr_driver-ab.py /s/b/r1.spec.json
