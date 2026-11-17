#!/usr/bin/env python3
"""Generate a scoped, paper-only Compose stack from the pinned Hummingbot API source."""
from __future__ import annotations

import argparse
from pathlib import Path
import yaml


def absolute_volume(value:str,api_root:Path)->str:
    source,separator,target=value.partition(":")
    if not separator:
        return value
    if source.startswith("./"):
        source=str((api_root/source[2:]).resolve())
    return f"{source}:{target}"


def main()->int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-repo",type=Path,default=Path("../spectra-hummingbot-api"))
    parser.add_argument("--output",type=Path,default=Path("docker-compose.alpha2.yml"))
    args=parser.parse_args()
    core_root=Path(__file__).resolve().parents[1]
    api_root=args.api_repo.resolve()
    source=api_root/"docker-compose.yml"
    if not source.is_file():
        raise SystemExit(f"API Compose source missing: {source}")
    data=yaml.safe_load(source.read_text(encoding="utf-8"))
    services=data.get("services",{})
    required={"hummingbot-api","emqx","postgres"}
    if not required.issubset(services):
        raise SystemExit(f"API Compose is missing required services: {sorted(required-services)}")

    api=services["hummingbot-api"]
    api["container_name"]="spectra-alpha2-hummingbot-api"
    api["build"]={"context":str(api_root),"dockerfile":"Dockerfile"}
    api["env_file"]=[str(api_root/".env")]
    api["ports"]=["127.0.0.1:8002:8000"]
    api["volumes"]=[
        absolute_volume(volume,api_root)
        for volume in api.get("volumes",[])
        if "/var/run/docker.sock" not in volume
        and not volume.startswith("/proc:")
        and not volume.startswith("/etc/hostname:")
    ]
    api.pop("extra_hosts",None)
    environment=[]
    for entry in api.get("environment",[]):
        if isinstance(entry,str):
            key=entry.split("=",1)[0]
            if key in {"HOST_PROC","HOST_DISK_PATH","GATEWAY_URL"}:
                continue
            if key=="DATABASE_URL":
                environment.append("DATABASE_URL=postgresql+asyncpg://hbot:${POSTGRES_PASSWORD}@postgres:5432/hummingbot_api")
                continue
            environment.append(entry)
        else:
            environment.append(entry)
    api["environment"]=environment
    api["depends_on"]={
        "postgres":{"condition":"service_healthy"},
        "emqx":{"condition":"service_healthy"},
    }
    api["healthcheck"]={
        "test":[
            "CMD-SHELL",
            "python -c \"import os,base64,urllib.request; r=urllib.request.Request('http://127.0.0.1:8000/spectra/health'); r.add_header('Authorization','Basic '+base64.b64encode((os.environ['USERNAME']+':'+os.environ['PASSWORD']).encode()).decode()); urllib.request.urlopen(r,timeout=5).read()\"",
        ],
        "interval":"10s","timeout":"7s","retries":30,"start_period":"60s",
    }

    broker=services["emqx"]
    broker["container_name"]="spectra-alpha2-broker"
    broker["image"]="docker.io/emqx/emqx:5.10.5"
    broker["ports"]=["127.0.0.1:1884:1883","127.0.0.1:18084:18083"]
    broker["volumes"]=[
        absolute_volume(volume,api_root)
        for volume in broker.get("volumes",[])
    ]

    postgres=services["postgres"]
    postgres["container_name"]="spectra-alpha2-postgres"
    postgres["image"]="docker.io/library/postgres:16.10"
    postgres["ports"]=[]
    postgres["environment"]=[
        "POSTGRES_DB=hummingbot_api",
        "POSTGRES_USER=hbot",
        "POSTGRES_PASSWORD=${POSTGRES_PASSWORD}",
        "POSTGRES_INITDB_ARGS=--encoding=UTF8",
    ]
    postgres["volumes"]=[
        absolute_volume(volume,api_root)
        for volume in postgres.get("volumes",[])
    ]

    services["spectra-core"]={
        "container_name":"spectra-alpha2-core",
        "image":"localhost/spectra-finance/spectra-core-alpha2:local",
        "build":{"context":str(core_root),"dockerfile":"Dockerfile"},
        "restart":"unless-stopped",
        "env_file":[str(core_root/".env.core")],
        "environment":{
            "SPECTRA_HB_API_URL":"http://hummingbot-api:8000",
            "SPECTRA_DB_PATH":"/data/spectra_alpha2.sqlite3",
        },
        "volumes":["spectra-alpha2-core-data:/data"],
        "depends_on":{"hummingbot-api":{"condition":"service_healthy"}},
        "networks":["emqx-bridge"],
        "healthcheck":{
            "test":["CMD","python","-m","spectra_alpha2","status"],
            "interval":"30s","timeout":"10s","retries":3,"start_period":"30s",
        },
    }

    data["networks"]={"emqx-bridge":{"driver":"bridge"}}
    data["volumes"]={
        "spectra-alpha2-emqx-data":{},
        "spectra-alpha2-emqx-log":{},
        "spectra-alpha2-emqx-etc":{},
        "spectra-alpha2-postgres-data":{},
        "spectra-alpha2-core-data":{},
    }
    # Rewrite named-volume references in each service to Alpha 2.0-scoped names.
    rename={
        "emqx-data":"spectra-alpha2-emqx-data",
        "emqx-log":"spectra-alpha2-emqx-log",
        "emqx-etc":"spectra-alpha2-emqx-etc",
        "postgres-data":"spectra-alpha2-postgres-data",
    }
    for service in services.values():
        for index,volume in enumerate(service.get("volumes",[])):
            if isinstance(volume,str):
                for old,new in rename.items():
                    if volume.startswith(old+":"):
                        service["volumes"][index]=volume.replace(old+":",new+":",1)
    text=yaml.safe_dump(data,sort_keys=False)
    forbidden=("/var/run/docker.sock","host.docker.internal","host-gateway","/proc:/host/proc","/etc/hostname:")
    if any(token in text for token in forbidden):
        raise SystemExit("Generated Alpha 2.0 Compose still contains an unsafe host integration.")
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(text,encoding="utf-8")
    print(f"Generated isolated Alpha 2.0 Compose file: {args.output.resolve()}")
    print("Host ports: API 127.0.0.1:8002, MQTT 127.0.0.1:1884, dashboard 127.0.0.1:18084.")
    print("No Docker socket, host /proc, host disk probe, or host-gateway mount.")
    return 0


if __name__=="__main__":
    raise SystemExit(main())
