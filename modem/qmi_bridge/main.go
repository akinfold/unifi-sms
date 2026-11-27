// Relay one SSH session to the existing libqmi proxy on the modem host.
package main

import (
	"io"
	"net"
	"os"
	"time"
)

func main() {
	conn, err := net.DialTimeout("unix", "@qmi-proxy", 5*time.Second)
	if err != nil {
		os.Exit(1)
	}
	defer conn.Close()
	conn.SetDeadline(time.Now().Add(2 * time.Minute))
	go func() {
		io.Copy(conn, os.Stdin)
		conn.Close()
	}()
	io.Copy(os.Stdout, conn)
}
