// Package config parses the shared config.yaml (same file the v1 Python
// collector reads — one config, two consumers during the migration).
package config

import (
	"fmt"
	"net/url"
	"os"

	"gopkg.in/yaml.v3"
)

type Sequence struct {
	Index         int16  `yaml:"index"`
	Name          string `yaml:"name"`
	IsProduction  bool   `yaml:"is_production"`
	CycleStart    string `yaml:"cycle_start_step"`
	CycleComplete string `yaml:"cycle_complete_step"`
}

type EM struct {
	Station     string     `yaml:"station"`
	DisplayName string     `yaml:"display_name"`
	EMDBPath    string     `yaml:"em_db_path"`
	EMLabel     string     `yaml:"em_label"`
	Enabled     *bool      `yaml:"enabled"`
	Sequences   []Sequence `yaml:"sequences"`
}

type Line struct {
	Name        string `yaml:"name"`
	OPCEndpoint string `yaml:"opc_endpoint"`
	Enabled     *bool  `yaml:"enabled"`
	EMs         []EM   `yaml:"equipment_modules"`
}

type Telemetry struct {
	Enabled    bool `yaml:"enabled"`
	ListenPort int  `yaml:"listen_port"`
}

type Config struct {
	Telemetry Telemetry `yaml:"telemetry"`
	Lines     []Line    `yaml:"plcs"`
}

func (e EM) IsEnabled() bool   { return e.Enabled == nil || *e.Enabled }
func (l Line) IsEnabled() bool { return l.Enabled == nil || *l.Enabled }

// Host extracts the PLC IP from the opc endpoint — datagram source IPs are
// matched against it.
func (l Line) Host() string {
	u, err := url.Parse(l.OPCEndpoint)
	if err != nil || u.Hostname() == "" {
		return ""
	}
	return u.Hostname()
}

func Load(path string) (*Config, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var c Config
	if err := yaml.Unmarshal(raw, &c); err != nil {
		return nil, fmt.Errorf("parse %s: %w", path, err)
	}
	if c.Telemetry.ListenPort == 0 {
		c.Telemetry.ListenPort = 15020
	}
	return &c, nil
}
