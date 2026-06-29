# Running LIOS Precompute on UCD Sonic HPC

This guide runs `precompute_hpc.py` through Slurm using
`run_precompute.slurm`. It follows UCD's
[Sonic user guide](https://ucdprod.service-now.com/itucd/en/how-do-i-get-started-using-sonic-hpc?id=kb_article_view&sysparm_article=KB0011715)
and is specific to the current LIOS experiment configuration.

## 1. Prerequisites

- Request a Sonic HPC account through the UCD IT Support Hub.
- Connect to the UCD Staff or Research VPN when accessing Sonic off campus.
- Have the LIOS repository in your Sonic home directory.
- Do not run the precompute directly on the login node. Submit it through Slurm.

UCD documents a home-directory quota of approximately 50 GB. Use scratch space
for generated contact plans and traffic schedules. Scratch is temporary storage,
not long-term storage, and files not modified for six months may be removed.

## 2. Connect to Sonic

From Linux, macOS, or a Windows SSH client:

```bash
ssh YOUR_UCD_USERNAME@login.ucd.ie
```

Your home directory is normally `/home/people/YOUR_UCD_USERNAME`, and scratch
storage is available under `/scratch/YOUR_UCD_USERNAME` or through the `scratch`
link in your home directory.

## 3. Prepare the repository

The supplied Slurm script expects the repository to be the directory from which
`sbatch` is called. For the conventional `~/LIOS` location:

```bash
cd ~/LIOS
mkdir -p logs
```

The `logs` directory must exist before submission because Slurm opens the output
and error files before the job script starts.

Check which Python modules Sonic currently provides:

```bash
module avail python
```

The current script loads:

```text
python/3.11.9-gcc-11.5.0-lcos74i
```

If that exact module is unavailable, edit the `module load` line in
`run_precompute.slurm` to use a listed Python 3.11 module.

Create the environment once on the login node:

```bash
cd ~/LIOS
module load python/3.11.9-gcc-11.5.0-lcos74i
python -m venv .env
source .env/bin/activate
python -m pip install --upgrade pip
python -m pip install -r lios/requirements.txt
```

Do not recreate or reinstall the environment inside every batch job.

## 4. Review the Slurm request

The supplied script requests:

| Resource | Value |
|---|---:|
| Partition | `cs` |
| Nodes | 1 |
| Slurm tasks | 1 |
| CPUs for the Python process | 32 |
| Wall time | 12 hours |
| Explicit memory | None; use partition default |
| Cache directory | `/scratch/$USER/lios_cache` |

Before submitting, update `#SBATCH --mail-user` in `run_precompute.slurm` if the
configured UCD address is not yours.

The UCD guide states that School of Computer Science contributors use the `cs`
partition. Users who are not in that contributor group should remove
`#SBATCH --partition=cs` and use the standard queue assigned to their account.

The normal shared-user limit is documented as 48 concurrently used cores, so the
32-core request remains within that limit. Requesting fewer cores can reduce queue
time. Override the request without editing the file using, for example:

```bash
sbatch --cpus-per-task=16 run_precompute.slurm
```

`precompute_hpc.py` receives `$SLURM_CPUS_PER_TASK`, so its worker count follows
the Slurm CPU allocation automatically.

## 5. Submit the job

From the repository root:

```bash
cd ~/LIOS
mkdir -p logs
sbatch run_precompute.slurm
```

Successful submission prints a job ID:

```text
Submitted batch job 123456
```

The job precomputes the current experiment inputs:

| Setting | Value |
|---|---:|
| Epoch | 2026-06-14 00:00:00 UTC |
| Duration | 86,400 seconds |
| Contact step | 30 seconds |
| ISL search range | 4,000 km |
| Traffic seed | 42 |
| Global traffic rate | 0.3 flows/s |
| Constellations | All TLE files under `lios/data/tles` |
| Operator traffic weights | Derived from each loaded constellation size |

With the current dataset this means 12,345 satellites across 18 operators. The
operator weights are normalized satellite counts and produce cache weight hash
`863a161b79`.

## 6. Monitor the job

List your queued and running jobs:

```bash
squeue -u "$USER"
```

Follow standard output and errors, replacing `JOB_ID`:

```bash
tail -f logs/precompute_JOB_ID.out
tail -f logs/precompute_JOB_ID.err
```

After completion, inspect status, elapsed time, and peak memory:

```bash
sacct -j JOB_ID --format=JobID,JobName,Partition,State,Elapsed,MaxRSS,ExitCode
```

Cancel a queued or running job:

```bash
scancel JOB_ID
```

## 7. Find and use the results

Generated artifacts are written to:

```text
/scratch/$USER/lios_cache
```

Inspect them with:

```bash
du -sh /scratch/$USER/lios_cache
ls -lh /scratch/$USER/lios_cache
```

The important final artifacts are:

```text
cp_24h_step30_range4000.csv
cp_24h_step30_range4000_meta.json
tf_pair_v2_e20260614T000000_d0.500_w863a161b79_24h_s42_r0.300000_step30_range4000.json
```

Copy the final contact plan, metadata, and traffic schedule into the cache used
by `run_experiments.py`:

```bash
cd ~/LIOS
mkdir -p lios/cache
cp /scratch/$USER/lios_cache/cp_24h_step30_range4000.csv lios/cache/
cp /scratch/$USER/lios_cache/cp_24h_step30_range4000_meta.json lios/cache/
cp /scratch/$USER/lios_cache/tf_pair_v2_e20260614T000000_d0.500_w863a161b79_24h_s42_r0.300000_step30_range4000.json lios/cache/
```

The experiment runner validates the contact epoch and uses the epoch and weight
hash in the traffic filename, preventing incompatible caches from being loaded.

## 8. Troubleshooting

### Memory specification cannot be satisfied

The supplied script deliberately omits `#SBATCH --mem`. Sonic previously rejected
the explicit `--mem=32G` request on the `cs` partition. Submit the current script:

```bash
sbatch run_precompute.slurm
```

If the job later fails because it genuinely exhausts memory, inspect `MaxRSS` with
`sacct`. UCD documents high-memory submission using:

```bash
sbatch --constraint=highmem run_precompute.slurm
```

Use high-memory nodes only when the measured workload requires them.

### Invalid partition or account

Check the partitions available to your account:

```bash
sinfo
scontrol show partition
```

If you are not a Computer Science contributor, remove the `cs` partition line or
ask UCD Research IT which standard partition applies to your account.

### Python module not found

```bash
module avail python
```

Update the module name in both the one-time setup commands and
`run_precompute.slurm`.

### Virtual environment not found

The job expects `.env/bin/activate` inside the submitted repository directory.
Create it using the commands in section 3, or update the activation path.

### Job remains pending

Show Slurm's pending reason:

```bash
squeue -j JOB_ID -o "%.18i %.9P %.24j %.2t %.10M %.6D %R"
```

Large CPU requests can wait longer. Retry with 16 or 8 CPUs if runtime permits.

## UCD references

- [How do I get started using Sonic HPC?](https://ucdprod.service-now.com/itucd/en/how-do-i-get-started-using-sonic-hpc?id=kb_article_view&sysparm_article=KB0011715)
- [UCD Sonic HPC overview](https://www.ucd.ie/itservices/ourservices/researchit/researchcomputing/sonichpc/)
- [UCD IT Support Hub](https://www.ucd.ie/ithelp)
